from typing import Optional
import os
from backend.rayserve_vllm.prompt_format import ModelConfig
import logging
import traceback
def load_model_config(model_id:str, config_path:Optional[str]=None) -> ModelConfig:
    """Load model config with enhanced logging."""
    logger = logging.getLogger("ray.serve")
    
    if not config_path:
        dir_path = os.path.dirname(os.path.realpath(__file__))
        logger.info(f"Base directory: {dir_path}")
        config_path = os.path.join(dir_path, 'models')
        logger.info(f"Looking for models in: {config_path}")
        if not os.path.isdir(config_path):
            config_path = os.path.join(os.path.dirname(dir_path), 'models')
            logger.info(f"Alternate models path: {config_path}")
    
    # Create sanitized filename
    sanitized_model_id = model_id.replace('/', '--')
    path = os.path.join(config_path, sanitized_model_id) + '.yaml'
    logger.info(f"Looking for config file at: {path}")
    
    if not os.path.exists(path):
        logger.error(f"Config file not found: {path}")
        raise FileNotFoundError(f"No config file found for model {model_id} at {path}")
    
    try:
        with open(path, "r") as stream:
            config = ModelConfig.parse_yaml(stream)
            logger.info(f"Loaded config for {model_id}: {config}")
            return config
    except Exception as e:
        logger.error(f"Error loading config from {path}: {str(e)}")
        logger.error(traceback.format_exc())
        raise