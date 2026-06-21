"""Neuralwatt Cloud provider profile."""

from providers import register_provider
from providers.base import ProviderProfile


neuralwatt = ProviderProfile(
    name="neuralwatt",
    aliases=("neural-watt", "nwatt"),
    display_name="Neuralwatt",
    description="Neuralwatt Cloud OpenAI-compatible inference with energy metrics",
    signup_url="https://portal.neuralwatt.com/",
    env_vars=("NEURALWATT_API_KEY", "NEURALWATT_BASE_URL"),
    base_url="https://api.neuralwatt.com/v1",
    auth_type="api_key",
    default_aux_model="glm-5-fast",
    fallback_models=(
        "moonshotai/Kimi-K2.5",
        "qwen3.6-35b-fast",
        "qwen3.5-397b-fast",
        "kimi-k2.6-fast",
        "glm-5.2-fast",
        "kimi-k2.5-fast",
        "glm-5.2-short",
        "glm-5.2-short-fast",
        "kimi-k2.7-code",
        "qwen3.5-397b",
        "kimi-k2.6",
        "glm-5.2",
        "qwen3.6-35b",
    ),
)

register_provider(neuralwatt)
