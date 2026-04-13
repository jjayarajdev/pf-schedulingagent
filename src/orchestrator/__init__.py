"""Orchestrator assembly — creates and caches the AgentSquad with 3 agents."""

import logging
from functools import lru_cache

from agent_squad.classifiers import BedrockClassifier, BedrockClassifierOptions
from agent_squad.orchestrator import AgentSquad
from agent_squad.storage import InMemoryChatStorage
from agent_squad.types import AgentSquadConfig

from config import get_settings
from orchestrator.agents.chitchat_agent import create_chitchat_agent
from orchestrator.agents.scheduling_agent import create_scheduling_agent
from orchestrator.agents.weather_agent import create_weather_agent

logger = logging.getLogger(__name__)


def _create_storage():
    """Create conversation storage — DynamoDB with InMemory fallback."""
    settings = get_settings()
    if not settings.use_dynamodb_storage:
        logger.info("Using InMemory storage (USE_DYNAMODB_STORAGE=false)")
        return InMemoryChatStorage()
    try:
        import boto3
        from agent_squad.storage import DynamoDbChatStorage

        ddb = boto3.client("dynamodb", region_name=settings.aws_region)
        ddb.describe_table(TableName=settings.session_table_name)

        storage = DynamoDbChatStorage(
            table_name=settings.session_table_name,
            region=settings.aws_region,
            ttl_key="ttl",
            ttl_duration=86400,
        )
        logger.info("Using DynamoDB storage (table: %s)", settings.session_table_name)
        return storage
    except Exception:
        logger.exception(
            "DynamoDB storage unavailable (table: %s), falling back to InMemory",
            settings.session_table_name,
        )
        return InMemoryChatStorage()


@lru_cache(maxsize=1)
def get_orchestrator() -> AgentSquad:
    """Create and cache the AgentSquad orchestrator with 3 agents.

    SchedulingAgent is the default (unclassified queries fall through to scheduling).
    """
    settings = get_settings()

    scheduling_agent = create_scheduling_agent()
    chitchat_agent = create_chitchat_agent()
    weather_agent = create_weather_agent()

    config = AgentSquadConfig(
        LOG_AGENT_CHAT=True,
        LOG_EXECUTION_TIMES=True,
        USE_DEFAULT_AGENT_IF_NONE_IDENTIFIED=True,
        MAX_MESSAGE_PAIRS_PER_AGENT=settings.max_message_pairs,
        GENERAL_ROUTING_ERROR_MSG_MESSAGE=(
            "Sorry, I ran into an issue processing your request. "
            "Please try again, or I can start fresh if you'd like."
        ),
    )

    storage = _create_storage()

    classifier = BedrockClassifier(
        options=BedrockClassifierOptions(
            model_id=settings.classifier_model_id,
            region=settings.aws_region,
        )
    )

    orchestrator = AgentSquad(
        options=config,
        storage=storage,
        classifier=classifier,
        default_agent=scheduling_agent,
    )
    orchestrator.add_agent(scheduling_agent)
    orchestrator.add_agent(chitchat_agent)
    orchestrator.add_agent(weather_agent)

    logger.info("Orchestrator initialized with Scheduling + Chitchat + Weather agents")
    return orchestrator
