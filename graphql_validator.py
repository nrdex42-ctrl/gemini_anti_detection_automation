"""Request-level validation for GraphQL mutations."""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional, Set, Tuple

logger = logging.getLogger(__name__)


class GraphQLRequestValidator:
    """Validates GraphQL mutations for safety and correctness."""
    
    # Known safe doc_ids (for logging/auditing). Any valid numeric id is
    # accepted by validate_mutation() so live-scraped doc_ids are not blocked.
    KNOWN_DOC_IDS: set = {
        '7711610262198779',  # ComposerStoryCreateMutation (2024 fallback)
        'doc-1',            # Mock mutation for unit testing
    }
    
    # Variable schema definition (shared across all ComposerStoryCreate variants)
    _COMPOSER_SCHEMA = {
        'input.composer_entry_point': str,
        'input.composer_source_surface': str,
        'input.message.text': str,
        'input.message.ranges': list,
        'input.idempotence_token': str,
        'input.actor_id': str,
        'input.client_mutation_id': str,
        'input.source': str,
        'input.attachments': (list, type(None)),
    }
    
    # Constraints
    CONSTRAINTS = {
        'input.message.text': {'max_length': 5000, 'min_length': 0},  # 0: allow image-only posts
        'input.idempotence_token': {'min_length': 32},
        'input.actor_id': {'pattern': r'^\d+$'},
    }

    @classmethod
    async def validate_mutation(
        cls,
        doc_id: str,
        variables: Dict[str, Any],
    ) -> Tuple[bool, str]:
        """
        Validate GraphQL mutation before sending.
        
        Accepts any numeric doc_id (Facebook deploys new ones frequently).
        Non-numeric doc_ids are rejected; known doc_ids are logged.
        Returns (success: bool, message: str)
        """
        # Allow any numeric doc_id (live-scraped values change every few days)
        # and also allow the test mock 'doc-1'.
        if doc_id != 'doc-1' and not str(doc_id or '').isdigit():
            return False, f"doc_id must be numeric: {doc_id!r}"

        if doc_id not in cls.KNOWN_DOC_IDS:
            logger.info(
                "GraphQLRequestValidator: accepting previously-unseen doc_id=%s "
                "(live-scraped — this is expected when Facebook deploys).",
                doc_id,
            )
        
        # Validate variables against the composer schema
        schema = cls._COMPOSER_SCHEMA
        for field_path, expected_type in schema.items():
            value = cls._get_nested_value(variables, field_path)
            
            if value is None:
                if expected_type != (list, type(None)):
                    return False, f"Missing required field: {field_path}"
                continue
            
            if isinstance(expected_type, tuple):
                if not isinstance(value, expected_type):
                    return False, f"Invalid type for {field_path}: {type(value).__name__}"
            else:
                if not isinstance(value, expected_type):
                    return False, f"Invalid type for {field_path}: {type(value).__name__} (expected {expected_type.__name__})"
        
        # Validate constraints
        is_valid, constraint_msg = await cls._validate_constraints(variables)
        if not is_valid:
            return False, constraint_msg
        
        return True, "Valid"
    
    @classmethod
    async def validate_variables(cls, variables: Dict[str, Any]) -> Tuple[bool, str]:
        """Validate mutation variables independently."""
        if not isinstance(variables, dict):
            return False, "Variables must be a dictionary"
        
        input_obj = variables.get('input')
        if not isinstance(input_obj, dict):
            return False, "Missing or invalid 'input' field in variables"
        
        # Check required fields
        required_fields = {
            'composer_entry_point',
            'composer_source_surface',
            'message',
            'idempotence_token',
            'actor_id',
            'client_mutation_id',
            'source',
        }
        
        missing = required_fields - set(input_obj.keys())
        if missing:
            return False, f"Missing required fields: {', '.join(sorted(missing))}"
        
        # Validate message structure
        message = input_obj.get('message')
        if not isinstance(message, dict):
            return False, "message must be a dictionary"
        
        if 'text' not in message or 'ranges' not in message:
            return False, "message must have 'text' and 'ranges' fields"
        
        if not isinstance(message['ranges'], list):
            return False, "message.ranges must be a list"
        
        return True, "Valid"
    
    @classmethod
    async def _validate_constraints(cls, variables: Dict[str, Any]) -> Tuple[bool, str]:
        """Validate field constraints."""
        text = cls._get_nested_value(variables, 'input.message.text', '')
        
        # Caption length constraint
        text_constraint = cls.CONSTRAINTS.get('input.message.text', {})
        if len(text) < text_constraint.get('min_length', 0):
            return False, f"Caption too short: {len(text)} chars (min: {text_constraint.get('min_length')})"
        if len(text) > text_constraint.get('max_length', 5000):
            return False, f"Caption too long: {len(text)} chars (max: {text_constraint.get('max_length')})"
        
        # Idempotence token length
        token = cls._get_nested_value(variables, 'input.idempotence_token', '')
        token_constraint = cls.CONSTRAINTS.get('input.idempotence_token', {})
        if len(token) < token_constraint.get('min_length', 32):
            return False, f"Idempotence token too short: {len(token)} chars (min: 32)"
        
        # Actor ID validation (must be numeric)
        actor_id = str(cls._get_nested_value(variables, 'input.actor_id', ''))
        if actor_id and not actor_id.isdigit():
            return False, f"actor_id must be numeric: {actor_id}"
        
        return True, "Valid"
    
    @staticmethod
    def _get_nested_value(obj: Dict[str, Any], path: str, default: Any = None) -> Any:
        """Get nested value using dot notation."""
        parts = path.split('.')
        current = obj
        
        for part in parts:
            if isinstance(current, dict):
                current = current.get(part)
                if current is None:
                    return default
            else:
                return default
        
        return current


class PermissionValidator:
    """Validates permissions for GraphQL mutations."""
    
    # Define which mutations require which permissions
    MUTATION_PERMISSIONS = {
        '7711610262198779': {  # ComposerStoryCreateMutation
            'required': {'post:create', 'token:valid'},
            'forbidden_statuses': {'BANNED', 'SUSPENDED', 'RESTRICTED'},
        }
    }
    
    @classmethod
    async def check_permissions(
        cls,
        doc_id: str,
        account_status: str,
        available_permissions: Set[str],
    ) -> Tuple[bool, str]:
        """Check if account has required permissions for mutation."""
        if doc_id not in cls.MUTATION_PERMISSIONS:
            return False, f"Unknown mutation: {doc_id}"
        
        perms = cls.MUTATION_PERMISSIONS[doc_id]
        
        # Check forbidden statuses
        if account_status in perms.get('forbidden_statuses', set()):
            return False, f"Account status '{account_status}' is forbidden"
        
        # Check required permissions
        required = perms.get('required', set())
        missing = required - available_permissions
        if missing:
            return False, f"Missing permissions: {', '.join(sorted(missing))}"
        
        return True, "Permitted"
