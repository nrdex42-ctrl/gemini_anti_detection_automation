"""CLI entry point and service runner for the automation package.

Provides:
    - extract-tokens: Extract Facebook tokens via Playwright
    - post: Post to a single page
    - post-batch: Process a JSON batch file of posts
    - check-health: Print health snapshot for one or all accounts
    - run-worker: Run the continuous queue worker loop
    - print-config: Print non-secret runtime configuration
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import AppConfig, SafetyConfig

logger = logging.getLogger(__name__)


def _get_redis_client(config: AppConfig) -> Any:
    """Create a Redis client from configuration."""
    if not config.redis_url:
        raise RuntimeError(
            'REDIS_URL is required. Set it in your environment: '
            'export REDIS_URL=redis://localhost:6379/0'
        )
    try:
        import redis.asyncio as aioredis  # type: ignore[reportMissingImports]
    except ImportError:
        import redis as aioredis  # type: ignore[reportMissingImports, no-redef]
    return aioredis.from_url(config.redis_url, decode_responses=False)


async def cmd_extract_tokens(args: argparse.Namespace, config: AppConfig) -> int:
    """Extract Facebook tokens from cookies via Playwright."""
    from .browser_fallback import BrowserTokenExtractor
    from .models import IdentityContext
    from .tokens import TokenVault

    cookies_path = Path(args.cookies_file)
    if not cookies_path.exists():
        print(f'Error: cookies file not found: {cookies_path}', file=sys.stderr)
        return 1

    cookies_json = cookies_path.read_text(encoding='utf-8')
    redis_client = _get_redis_client(config)

    try:
        # Build a minimal identity for token extraction
        identity = IdentityContext(
            account_id=args.account_id,
            proxy_url=args.proxy_url or 'http://127.0.0.1:8080',
            user_agent=(
                'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                '(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36'
            ),
            chrome_version='126.0.0.0',
        )

        vault = TokenVault(redis_client)
        extractor = BrowserTokenExtractor(vault, identity)

        print(f'Extracting tokens for account: {args.account_id}')
        tokens = await extractor.extract_tokens(cookies_json)

        if tokens:
            print('✓ Tokens extracted successfully:')
            # Mask sensitive values in output
            display = {
                'fb_dtsg': f'{str(tokens.get("fb_dtsg", ""))[:8]}...',
                'lsd': f'{str(tokens.get("lsd", ""))[:8]}...',
                'user_id': tokens.get('user_id', ''),
                'revision': tokens.get('revision', ''),
                'timestamp': tokens.get('timestamp', ''),
            }
            print(json.dumps(display, indent=2))
            return 0
        else:
            print('✗ Token extraction failed', file=sys.stderr)
            return 1
    finally:
        close = getattr(redis_client, 'close', None)
        if close:
            await close()


async def cmd_post(args: argparse.Namespace, config: AppConfig) -> int:
    """Post to a single Facebook page."""
    from .identity import IdentityRegistry
    from .models import PostJob, PostResult
    from .worker import HTTPWorker

    redis_client = _get_redis_client(config)

    try:
        registry = IdentityRegistry(redis_client)
        identity = await registry.get(args.account_id)
        if identity is None:
            print(f'Error: no identity registered for account {args.account_id}', file=sys.stderr)
            return 1

        job = PostJob(
            account_id=args.account_id,
            page_id=args.page_id,
            caption=args.caption,
            media_url=args.media_path or None,
            post_type=args.post_type or 'text',
        )

        worker = HTTPWorker(redis_client, identity, config)
        result: PostResult = await worker.process_job(job)

        print(json.dumps(result.to_dict(), indent=2, default=str))
        return 0 if result.success else 1
    finally:
        close = getattr(redis_client, 'close', None)
        if close:
            await close()


async def cmd_post_batch(args: argparse.Namespace, config: AppConfig) -> int:
    """Process a JSON batch file of posts."""
    from .models import PostJob
    from .orchestrator import QueueOrchestrator

    batch_path = Path(args.batch_file)
    if not batch_path.exists():
        print(f'Error: batch file not found: {batch_path}', file=sys.stderr)
        return 1

    raw = json.loads(batch_path.read_text(encoding='utf-8'))
    if isinstance(raw, dict):
        raw = raw.get('posts', [raw])
    if not isinstance(raw, list):
        print('Error: batch file must contain a JSON array or {"posts": [...]}', file=sys.stderr)
        return 1

    redis_client = _get_redis_client(config)

    try:
        orchestrator = QueueOrchestrator(redis_client)
        jobs = [PostJob.from_dict(item) for item in raw]

        print(f'Dispatching {len(jobs)} jobs...')
        results = await orchestrator.dispatch_jobs(jobs, config=config)

        successes = sum(1 for r in results if r.success)
        failures = len(results) - successes
        print(f'\n✓ {successes} succeeded, ✗ {failures} failed')

        for i, result in enumerate(results):
            status_icon = '✓' if result.success else '✗'
            print(f'  {status_icon} [{i}] page={result.page_id} status={result.status} '
                  f'post_id={result.post_id or "N/A"} '
                  f'time={result.execution_time_ms}ms')

        return 0 if failures == 0 else 1
    finally:
        close = getattr(redis_client, 'close', None)
        if close:
            await close()


async def cmd_check_health(args: argparse.Namespace, config: AppConfig) -> int:
    """Print health snapshot for one or all accounts."""
    from .health import HealthMonitor

    redis_client = _get_redis_client(config)

    try:
        monitor = HealthMonitor(redis_client)

        if args.account_id:
            snapshot = await monitor.account_snapshot(args.account_id)
            print(json.dumps(snapshot.to_dict(), indent=2, default=str))
        else:
            global_snapshot = await monitor.global_snapshot()
            print(json.dumps(global_snapshot, indent=2, default=str))

        return 0
    finally:
        close = getattr(redis_client, 'close', None)
        if close:
            await close()


async def cmd_run_worker(args: argparse.Namespace, config: AppConfig) -> int:
    """Run the continuous queue worker loop."""
    from .lifecycle import ApplicationLifecycle
    from .runner import WorkerLoop

    redis_client = _get_redis_client(config)
    lifecycle = ApplicationLifecycle(redis_client)
    lifecycle.register_signal_handlers()

    worker_loop = WorkerLoop(redis_client, config=config)

    print(f'Worker started. Polling queue every {config.worker_poll_interval_seconds}s...')
    print(f'  Concurrency: {config.worker_concurrency}')
    print(f'  Private HTTP: {"enabled" if config.enable_private_facebook_http else "disabled"}')
    print(f'  Browser fallback: {"enabled" if config.enable_browser_fallback else "disabled"}')
    print('Press Ctrl+C to stop.')

    try:
        await worker_loop.run_forever(stop_event=lifecycle.shutdown_event)
    except KeyboardInterrupt:
        pass
    finally:
        await lifecycle.shutdown('CLI')

    return 0


async def cmd_quarantine_reset(args: argparse.Namespace, config: AppConfig) -> int:
    """Reset quarantine for an account (admin override)."""
    from .safety import QuarantineManager

    redis_client = _get_redis_client(config)

    try:
        qm = QuarantineManager(redis_client)
        level = await qm.get_level(args.account_id)
        print(f'Current quarantine level: {level.value}')

        if args.force:
            await qm.reset(args.account_id, admin_override=True)
            print(f'✓ Quarantine reset for {args.account_id}')
        else:
            try:
                await qm.reset(args.account_id, admin_override=False)
                print(f'✓ Quarantine expired and cleared for {args.account_id}')
            except RuntimeError as exc:
                print(f'✗ Cannot reset: {exc}', file=sys.stderr)
                print('  Use --force for admin override', file=sys.stderr)
                return 1

        return 0
    finally:
        close = getattr(redis_client, 'close', None)
        if close:
            await close()


def cmd_print_config(config: AppConfig) -> int:
    """Print non-secret runtime configuration."""
    safety = SafetyConfig()
    output = {
        'redis_url_configured': bool(config.redis_url),
        'proxy_count': len(config.proxy_pool),
        'worker_concurrency': config.worker_concurrency,
        'worker_poll_interval_seconds': config.worker_poll_interval_seconds,
        'max_concurrent_accounts_global': safety.max_concurrent_accounts_global,
        'max_concurrent_posts_per_account': safety.max_concurrent_posts_per_account,
        'min_interval_seconds': safety.min_interval_seconds,
        'browser_fallback_enabled': config.enable_browser_fallback,
        'max_browser_fallback_ratio': config.max_browser_fallback_ratio,
        'private_http_enabled': config.enable_private_facebook_http,
        'posts_per_hour': safety.posts_per_hour,
        'posts_per_day': safety.posts_per_day,
        'token_ttl_seconds': safety.token_ttl_seconds,
        'quarantine_soft_seconds': safety.quarantine_soft_seconds,
        'quarantine_hard_seconds': safety.quarantine_hard_seconds,
        'quarantine_severe_seconds': safety.quarantine_severe_seconds,
    }
    print(json.dumps(output, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser."""
    parser = argparse.ArgumentParser(
        prog='fb_automation',
        description='Guarded Facebook automation package — CLI utilities',
    )
    parser.add_argument('--log-level', default='INFO', help='Logging level')

    subparsers = parser.add_subparsers(dest='command', help='Available commands')

    # extract-tokens
    p_extract = subparsers.add_parser('extract-tokens', help='Extract FB tokens via Playwright')
    p_extract.add_argument('--account-id', required=True, help='Internal account ID')
    p_extract.add_argument('--cookies-file', required=True, help='Path to cookies JSON file')
    p_extract.add_argument('--proxy-url', help='Proxy URL (optional)')

    # post
    p_post = subparsers.add_parser('post', help='Post to a single page')
    p_post.add_argument('--account-id', required=True, help='Internal account ID')
    p_post.add_argument('--page-id', required=True, help='Facebook page ID')
    p_post.add_argument('--caption', required=True, help='Post caption text')
    p_post.add_argument('--media-path', help='Path to image/video file')
    p_post.add_argument('--post-type', choices=['text', 'image', 'video'], default='text')

    # post-batch
    p_batch = subparsers.add_parser('post-batch', help='Process a JSON batch of posts')
    p_batch.add_argument('--batch-file', required=True, help='Path to JSON batch file')

    # check-health
    p_health = subparsers.add_parser('check-health', help='Print account health snapshot')
    p_health.add_argument('--account-id', help='Account ID (omit for global snapshot)')

    # run-worker
    subparsers.add_parser('run-worker', help='Run continuous queue worker loop')

    # quarantine-reset
    p_qreset = subparsers.add_parser('quarantine-reset', help='Reset account quarantine')
    p_qreset.add_argument('--account-id', required=True, help='Account ID to reset')
    p_qreset.add_argument('--force', action='store_true', help='Admin override (ignore TTL)')

    # print-config
    subparsers.add_parser('print-config', help='Print non-secret runtime config')

    return parser


def main() -> int:
    """Main entry point."""
    parser = build_parser()
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, (args.log_level or 'INFO').upper(), logging.INFO),
        format='%(asctime)s %(levelname)-8s [%(name)s] %(message)s',
    )

    config = AppConfig()

    if args.command == 'print-config':
        return cmd_print_config(config)

    if args.command is None:
        parser.print_help()
        return 0

    # All other commands are async
    command_map = {
        'extract-tokens': cmd_extract_tokens,
        'post': cmd_post,
        'post-batch': cmd_post_batch,
        'check-health': cmd_check_health,
        'run-worker': cmd_run_worker,
        'quarantine-reset': cmd_quarantine_reset,
    }

    handler = command_map.get(args.command)
    if handler is None:
        parser.print_help()
        return 1

    try:
        return asyncio.run(handler(args, config))
    except KeyboardInterrupt:
        print('\nInterrupted.')
        return 130
    except Exception as exc:
        logger.exception('Command failed: %s', exc)
        print(f'Error: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
