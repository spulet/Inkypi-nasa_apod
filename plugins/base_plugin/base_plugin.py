# plugins/base_plugin/base_plugin.py

class BasePlugin:
    def __init__(self, config):
        self.config = config

    def generate_image(self, settings, device_config, inky_display=None):
        raise NotImplementedError("Plugins must implement generate_image")
