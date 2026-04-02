"""Environment config loader with Secrets Manager resolution and caching."""

import json
import logging
from functools import lru_cache

import boto3
from pydantic_settings import BaseSettings

logger = logging.getLogger(__name__)


_PF_API_URLS = {
    "dev": "https://api-cx-portal.dev.projectsforce.com",
    "qa": "https://api-cx-portal.qa.projectsforce.com",
    "uat": "https://api-cx-portal.qa.projectsforce.com",
    "staging": "https://api-cx-portal.staging.projectsforce.com",
    "prod": "https://api-cx-portal.apps.projectsforce.com",
}

# Vapi phone numbers per environment (Vapi-managed numbers don't send
# phoneNumber or assistantId in assistant-request, so we need a fallback)
_VAPI_PHONE_NUMBERS = {
    "dev": "+19566699322",
    "qa": "+14588990940",
}


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    # Environment
    environment: str = "dev"
    aws_region: str = "us-east-1"

    # Bedrock
    bedrock_model_id: str = "us.anthropic.claude-sonnet-4-20250514-v1:0"
    classifier_model_id: str = "us.anthropic.claude-sonnet-4-20250514-v1:0"

    # AgentSquad
    max_message_pairs: int = 50

    # DynamoDB table names (follows pf-syn-*-{env} convention)
    session_table_name: str = ""
    phone_creds_table: str = ""
    dynamodb_conversations_table: str = ""

    # ProjectsForce API — set PF_API_BASE_URL to override, otherwise derived from ENVIRONMENT
    pf_api_base_url: str = ""

    # Vapi
    vapi_secret_arn: str = ""
    vapi_assistants_table: str = ""  # assistant_id → phone_number mapping
    vapi_phone_number: str = ""  # Legacy fallback (prefer vapi_assistants_table)
    default_support_number: str = ""  # Fallback support number for call transfers

    # SMS (AWS End User Messaging / pinpoint-sms-voice-v2)
    sms_origination_number: str = ""
    sms_configuration_set: str = ""

    # Storage
    use_dynamodb_storage: bool = True

    # Dev server mode — enables login proxy + test client regardless of ENVIRONMENT
    dev_server: bool = False

    model_config = {"env_prefix": "", "case_sensitive": False}

    def model_post_init(self, __context):
        """Derive dynamic defaults from ENVIRONMENT if not explicitly set."""
        env = self.environment

        if not self.pf_api_base_url:
            self.pf_api_base_url = _PF_API_URLS.get(env, _PF_API_URLS["dev"])

        # DynamoDB tables: pf-syn-schedulingagents-sessions-{env}
        if not self.session_table_name:
            self.session_table_name = f"pf-syn-schedulingagents-sessions-{env}"
        if not self.phone_creds_table:
            self.phone_creds_table = f"pf-syn-schedulingagents-phone-creds-{env}"
        if not self.dynamodb_conversations_table:
            self.dynamodb_conversations_table = f"pf-syn-schedulingagents-conversations-{env}"
        if not self.vapi_assistants_table:
            self.vapi_assistants_table = f"pf-syn-schedulingagents-vapi-assistants-{env}"

        # Vapi phone number fallback
        if not self.vapi_phone_number:
            self.vapi_phone_number = _VAPI_PHONE_NUMBERS.get(env, "")

        # SMS configuration set: scheduling-agent-sms-config-{env}
        if not self.sms_configuration_set:
            self.sms_configuration_set = f"scheduling-agent-sms-config-{env}"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return cached Settings instance (singleton)."""
    return Settings()


class SecretsCache:
    """Resolves and caches Secrets Manager values."""

    def __init__(self, region: str | None = None):
        self._region = region or get_settings().aws_region
        self._client = None
        self._cache: dict[str, dict] = {}

    @property
    def client(self):
        if self._client is None:
            self._client = boto3.client("secretsmanager", region_name=self._region)
        return self._client

    def get_secret(self, secret_arn: str) -> dict:
        """Retrieve and cache a secret value. Returns parsed JSON dict."""
        if secret_arn in self._cache:
            return self._cache[secret_arn]

        if not secret_arn:
            return {}

        try:
            secret = self._get_secret_with_retry(secret_arn)
            self._cache[secret_arn] = secret
            logger.info("Resolved secret: %s", secret_arn.split(":")[-1])
            return secret
        except Exception:
            logger.exception("Failed to resolve secret: %s", secret_arn)
            return {}

    def _get_secret_with_retry(self, secret_arn: str) -> dict:
        """Call Secrets Manager (retries on transient errors)."""
        from observability.retry import retry_secrets

        @retry_secrets
        def _call():
            response = self.client.get_secret_value(SecretId=secret_arn)
            return json.loads(response["SecretString"])

        return _call()

    @property
    def vapi_api_key(self) -> str:
        secret = self.get_secret(get_settings().vapi_secret_arn)
        return secret.get("vapi_api_key", "")


@lru_cache(maxsize=1)
def get_secrets() -> SecretsCache:
    """Return cached SecretsCache instance (singleton)."""
    return SecretsCache()
