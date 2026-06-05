"""Diagnose browser File/Blob/set_input_files integrity.

This script intentionally does not use Facebook cookies or network access. It
checks whether the active Chromium build and init scripts can create, assign,
and read File/Blob objects without producing empty content.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional


CODEIUM_CHROMIUM = Path('/home/shabana/.codeium/ws-browser/chromium-1155/chrome-linux/chrome')
PLAYWRIGHT_CHROMIUM = Path('/home/shabana/.cache/ms-playwright/chromium-1117/chrome-linux/chrome')

SAFE_ARGS = [
    '--disable-blink-features=AutomationControlled',
    '--disable-dev-shm-usage',
    '--disable-default-apps',
    '--disable-extensions',
    '--disable-component-extensions-with-background-pages',
    '--disable-hang-monitor',
    '--disable-popup-blocking',
    '--disable-prompt-on-repost',
    '--disable-sync',
    '--disable-translate',
    '--metrics-recording-only',
    '--no-first-run',
    '--no-default-browser-check',
    '--password-store=basic',
    '--use-mock-keychain',
]

ENGINE_INIT_SCRIPT = """
(() => {
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined, configurable: true });
    for (const key of [
        '__webdriver_evaluate', '__selenium_evaluate', '__webdriver_unwrapped',
        '__driver_unwrapped', '__webdriver_script_fn', '__driver_evaluate',
        '__selenium_unwrapped', '__fxdriver_unwrapped', '__fxdriver_evaluate',
        '_Selenium_IDE_Recorder', '_selenium', 'calledSelenium',
        '__nightmare', '__phantomas', 'domAutomation', 'domAutomationController',
    ]) {
        try {
            Object.defineProperty(window, key, { get: () => undefined, configurable: true });
            delete window[key];
        } catch (_) {}
    }
    window.chrome = window.chrome || {};
    window.chrome.runtime = window.chrome.runtime || {
        connect: () => ({ onMessage: { addListener: () => {} } }),
        sendMessage: () => {},
        onMessage: { addListener: () => {} },
        id: undefined,
    };
})();
"""

DIAGNOSTIC_JS = """
async () => {
    const results = {};
    const describe = value => {
        try { return Function.prototype.toString.call(value).slice(0, 220); }
        catch (e) { return String(e && e.message || e).slice(0, 220); }
    };

    results.file_native = describe(File).includes('[native code]');
    results.file_source = describe(File);
    results.blob_native = describe(Blob).includes('[native code]');
    results.blob_source = describe(Blob);
    results.filereader_native = describe(FileReader).includes('[native code]');
    results.input_files_descriptor_native = Boolean(
        Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'files')?.get
    );

    try {
        const blob = new Blob(['hello world'], {type: 'text/plain'});
        results.blob_size_after_create = blob.size;
        results.blob_type = blob.type;
        const file = new File([blob], 'test.txt', {type: 'text/plain'});
        results.file_size_after_create = file.size;
        results.file_name = file.name;
        results.file_type = file.type;
        results.file_read_text = await file.text();
        results.file_read_success = results.file_read_text === 'hello world';
    } catch (e) {
        results.file_create_error = e && e.message || String(e);
        results.file_read_success = false;
    }
    return results;
}
"""

FILE_INPUT_SETUP_JS = """
() => {
    document.body.innerHTML = '';
    const input = document.createElement('input');
    input.type = 'file';
    input.id = 'test-upload';
    input.accept = 'image/*';
    document.body.appendChild(input);
}
"""

FILE_INPUT_READ_JS = """
async el => {
    if (!el.files || el.files.length === 0) {
        return {has_files: false, count: 0};
    }
    const file = el.files[0];
    const buffer = await file.arrayBuffer();
    const bytes = Array.from(new Uint8Array(buffer).slice(0, 16));
    let hash = 0;
    for (const byte of new Uint8Array(buffer)) {
        hash = ((hash * 31) + byte) >>> 0;
    }
    let dataUrlResult = {};
    try {
        dataUrlResult = await new Promise(resolve => {
            const reader = new FileReader();
            reader.onload = event => resolve({
                filereader_success: true,
                data_url_length: String(event.target.result || '').length,
            });
            reader.onerror = event => resolve({
                filereader_success: false,
                filereader_error: event.target.error?.name || 'read error',
            });
            reader.readAsDataURL(file);
        });
    } catch (e) {
        dataUrlResult = {filereader_success: false, filereader_error: e && e.message || String(e)};
    }
    return {
        has_files: true,
        count: el.files.length,
        name: file.name,
        size: file.size,
        type: file.type,
        last_modified_positive: file.lastModified > 0,
        array_buffer_size: buffer.byteLength,
        first_bytes: bytes,
        byte_hash: hash,
        ...dataUrlResult,
    };
}
"""


def _make_test_image() -> str:
    handle = tempfile.NamedTemporaryFile(prefix='fb_blob_diag_', suffix='.jpg', delete=False)
    handle.close()
    path = handle.name
    try:
        from PIL import Image

        Image.new('RGB', (160, 160), (24, 64, 96)).save(
            path,
            'JPEG',
            quality=90,
            progressive=False,
            subsampling=2,
        )
    except Exception:
        with open(path, 'wb') as file:
            file.write(b'\xff\xd8\xff\xe0' + b'fb-blob-diagnostic' * 64 + b'\xff\xd9')
    return path


async def _browser_stealth_script() -> str:
    try:
        from fb_automation.browser_stealth import BrowserStealth, StealthConfig

        stealth = BrowserStealth(StealthConfig())
        return stealth.build_init_script()
    except Exception:
        return ''


async def _run_variant(
    pw: Any,
    *,
    name: str,
    executable_path: Optional[Path],
    args: List[str],
    init_script: str = '',
    image_path: str,
) -> Dict[str, Any]:
    launch_options: Dict[str, Any] = {
        'headless': True,
        'args': list(args),
    }
    if executable_path is not None:
        launch_options['executable_path'] = str(executable_path)

    result: Dict[str, Any] = {
        'name': name,
        'executable_path': str(executable_path) if executable_path else '<playwright-default>',
        'args_count': len(args),
        'init_script_bytes': len(init_script or ''),
    }
    try:
        browser = await pw.chromium.launch(**launch_options)
        context = await browser.new_context()
        if init_script:
            await context.add_init_script(init_script)
        page = await context.new_page()
        await page.goto('about:blank')
        result['constructor'] = await page.evaluate(DIAGNOSTIC_JS)
        await page.evaluate(FILE_INPUT_SETUP_JS)
        locator = page.locator('#test-upload')
        await locator.set_input_files(image_path, timeout=5000)
        await page.wait_for_timeout(250)
        result['file_input'] = await locator.evaluate(FILE_INPUT_READ_JS)
        await context.close()
        await browser.close()
    except Exception as exc:
        result['exception'] = f'{type(exc).__name__}: {str(exc)[:400]}'
    return result


def _variant_passed(result: Dict[str, Any], expected_size: int) -> bool:
    constructor = result.get('constructor') or {}
    file_input = result.get('file_input') or {}
    return bool(
        not result.get('exception')
        and constructor.get('file_read_success') is True
        and file_input.get('has_files') is True
        and int(file_input.get('size') or -1) == expected_size
        and int(file_input.get('array_buffer_size') or -1) == expected_size
        and file_input.get('filereader_success') is True
    )


def _print_variant(result: Dict[str, Any], expected_size: int) -> None:
    ok = _variant_passed(result, expected_size)
    print(f"\n--- {result['name']} ---")
    print(f"executable: {result['executable_path']}")
    print(f"args_count: {result['args_count']} init_script_bytes: {result['init_script_bytes']}")
    if result.get('exception'):
        print(f"exception: {result['exception']}")
        return
    constructor = result.get('constructor') or {}
    file_input = result.get('file_input') or {}
    print(f"File native: {constructor.get('file_native')}")
    print(f"Blob native: {constructor.get('blob_native')}")
    print(f"FileReader native: {constructor.get('filereader_native')}")
    print(f"Created File size: {constructor.get('file_size_after_create')}")
    print(f"Created File readable: {constructor.get('file_read_success')}")
    print(f"Input has files: {file_input.get('has_files')} count={file_input.get('count')}")
    print(f"Input file size: {file_input.get('size')} expected={expected_size}")
    print(f"ArrayBuffer size: {file_input.get('array_buffer_size')}")
    print(f"FileReader success: {file_input.get('filereader_success')}")
    print(f"Byte hash: {file_input.get('byte_hash')}")
    print(f"PASS: {ok}")


async def main() -> int:
    parser = argparse.ArgumentParser(description='Diagnose Playwright browser File/Blob integrity.')
    parser.add_argument('--json-out', default='', help='Optional path for JSON results.')
    args = parser.parse_args()

    image_path = _make_test_image()
    expected_size = os.path.getsize(image_path)
    print('=' * 72)
    print('BROWSER FILE/BLOB INTEGRITY DIAGNOSTIC')
    print('=' * 72)
    print(f'test_image: {image_path}')
    print(f'test_image_size: {expected_size}')

    results: List[Dict[str, Any]] = []
    try:
        from playwright.async_api import async_playwright

        stealth_script = await _browser_stealth_script()
        async with async_playwright() as pw:
            variants = [
                {
                    'name': 'playwright_default_no_init',
                    'executable_path': None,
                    'args': [],
                    'init_script': '',
                },
                {
                    'name': 'playwright_default_safe_args',
                    'executable_path': None,
                    'args': SAFE_ARGS,
                    'init_script': '',
                },
                {
                    'name': 'codeium_no_init',
                    'executable_path': CODEIUM_CHROMIUM if CODEIUM_CHROMIUM.exists() else None,
                    'args': [],
                    'init_script': '',
                },
                {
                    'name': 'codeium_safe_args',
                    'executable_path': CODEIUM_CHROMIUM if CODEIUM_CHROMIUM.exists() else None,
                    'args': SAFE_ARGS,
                    'init_script': '',
                },
                {
                    'name': 'codeium_engine_init',
                    'executable_path': CODEIUM_CHROMIUM if CODEIUM_CHROMIUM.exists() else None,
                    'args': SAFE_ARGS,
                    'init_script': ENGINE_INIT_SCRIPT,
                },
                {
                    'name': 'codeium_browser_stealth',
                    'executable_path': CODEIUM_CHROMIUM if CODEIUM_CHROMIUM.exists() else None,
                    'args': SAFE_ARGS,
                    'init_script': stealth_script,
                },
            ]
            for variant in variants:
                if variant['name'].startswith('codeium') and not CODEIUM_CHROMIUM.exists():
                    results.append({
                        'name': variant['name'],
                        'executable_path': str(CODEIUM_CHROMIUM),
                        'exception': 'Codeium Chromium executable not found',
                        'args_count': len(variant['args']),
                        'init_script_bytes': len(variant['init_script'] or ''),
                    })
                    continue
                result = await _run_variant(pw, image_path=image_path, **variant)
                results.append(result)
                _print_variant(result, expected_size)
    finally:
        try:
            os.unlink(image_path)
        except OSError:
            pass

    summary = {
        'timestamp': time.strftime('%Y-%m-%dT%H:%M:%S'),
        'expected_size': expected_size,
        'results': results,
        'passed': {
            result['name']: _variant_passed(result, expected_size)
            for result in results
        },
    }
    print('\n' + '=' * 72)
    print('SUMMARY')
    print('=' * 72)
    print(json.dumps(summary['passed'], indent=2, sort_keys=True))

    if args.json_out:
        output_path = Path(args.json_out)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding='utf-8')
        print(f'json_out: {output_path}')

    return 0 if all(summary['passed'].values()) else 1


if __name__ == '__main__':
    raise SystemExit(asyncio.run(main()))
