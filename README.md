# Home Assistant Entity Manager Add-on

[![GitHub Release](https://img.shields.io/github/release/Skjall/home-assistant-entity-manager.svg?style=flat-square)](https://github.com/Skjall/home-assistant-entity-manager/releases)
[![License](https://img.shields.io/github/license/Skjall/home-assistant-entity-manager.svg?style=flat-square)](LICENSE)

A Home Assistant Add-on for standardizing and managing entity names according to a consistent, logical naming convention with an integrated Web UI.

## Features

- **Batch Entity Renaming**: Rename multiple entities according to a standardized pattern
- **Logical Naming Convention**: Follows the pattern `{area}.{device_type}.{location/name}`
- **Character Normalization**: Automatically normalizes special characters for entity IDs
- **Dependency Tracking**: Finds and updates entity references in automations, scenes, scripts and dashboards
- **Device Swap**: Replace a physical device with a new one — re-maps all references, carries over IDs/friendly names, and is crash-safe/resumable
- **Integration Bridge**: Native rename/removal for Zigbee2MQTT (via MQTT), Matter and ZHA — keeps Z2M friendly names in sync and prevents re-population after deletion
- **Z2M Name Drift**: Highlights devices whose Zigbee2MQTT name differs from the HA name and lets you sync them
- **Learn Translations**: Save a suffix translation to the mapping list directly from the entity editor
- **Label Management**: Track entity quality and processing status
- **Web Interface**: Visualize and manage entities through an intuitive UI
- **Safe Operations**: Preview and comprehensive validation before changes

## Requirements

- Home Assistant 2025.7.2 or newer
- Python 3.12 or 3.13

## Installation

[![Open your Home Assistant instance and show the add add-on repository dialog with a specific repository URL pre-filled.](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2FSkjall%2Fhome-assistant-entity-manager)

<details>
<summary>Manual Add-on installation</summary>

1. Navigate to Supervisor → Add-on Store
2. Click the three dots menu → Repositories
3. Add this repository: `https://github.com/Skjall/home-assistant-entity-manager`
4. Click "Add"
5. Find "Entity Manager" in the add-on store
6. Click on it and then click "Install"
7. Start the add-on
8. Click "OPEN WEB UI" to access the interface
</details>


## Usage

After installing and starting the Add-on:

1. Navigate to the sidebar in Home Assistant
2. Click on "Entity Manager"
3. Select an area from the dropdown
4. Optionally filter by domain (light, switch, sensor, etc.)
5. Preview entities that need renaming
6. Review dependency warnings (shows which automations/scenes use each entity)
7. Select entities to process
8. Click "Execute Changes" to apply

The web interface features:
- **Area-based navigation**: Browse entities organized by area
- **Domain filtering**: Filter entities by type (light, switch, sensor, etc.)
- **Visual indicators**: See which entities need renaming at a glance
- **Dependency detection**: See which automations and scenes use each entity
- **Safe preview**: Review all changes before applying them
- **Device management**: Rename devices directly from the interface
- **Custom overrides**: Set custom names for specific entities

## Naming Convention

Entities follow this pattern:
```
{area}.{device_type}.{location/name}
```

Examples:
- `office.light.ceiling` - Office ceiling light
- `living_room.sensor.temperature` - Living room temperature sensor
- `kitchen.switch.outlet_1` - Kitchen power outlet switch 1

## Configuration

### Naming Overrides

Create a `naming_overrides.json` file to customize naming:

```json
{
  "areas": {
    "area_id_here": {
      "name": "Custom Area Name"
    }
  },
  "devices": {
    "device_id_here": {
      "name": "Custom Device Name"
    }
  },
  "entities": {
    "entity_registry_id_here": {
      "name": "Custom Entity Name"
    }
  }
}
```

## Safety Features

1. **Dry Run Mode**: Always test with `--test` flag first
2. **Dependency Scanning**: Automatically finds and updates entity references
3. **Label System**: Track which entities have been processed
4. **Validation**: Comprehensive checks before applying changes
5. **WebSocket & REST API**: Reliable communication with Home Assistant

## Add-on Structure

```
├── config.json            # Add-on configuration
├── Dockerfile             # Add-on container definition
├── build.json             # Build configuration
├── repository.json        # Repository metadata
├── web_ui.py              # Flask web interface
├── entity_restructurer.py # Core renaming logic
├── dependency_scanner.py  # Find entity references
├── dependency_updater.py  # Update references
├── entity_registry.py     # Entity management
├── device_registry.py     # Device management
├── label_registry.py      # Label operations
└── templates/             # Web UI templates
```


## Troubleshooting

### Common Issues

#### Add-on won't start
If the Add-on fails to start:
1. Check the Add-on logs for error messages
2. Ensure Home Assistant 2025.7.2 or newer is installed
3. Verify the Add-on has the necessary permissions

#### Web UI not accessible
If you can't access the web interface:
1. Ensure the Add-on is running
2. Try restarting the Add-on
3. Check if Ingress is enabled in the Add-on configuration

#### Entity rename fails
If entity renaming fails:
1. Check the entity ID is valid
2. Ensure the entity is not locked or read-only
3. Check if the new entity ID already exists
4. Review Add-on logs for specific error messages

### Debug Logging

Check the Add-on logs in Supervisor → Entity Manager → Logs

## Development

### Building the Add-on Locally

```bash
# Build for your architecture
docker build --build-arg BUILD_FROM="ghcr.io/home-assistant/amd64-base-python:3.14-alpine3.20" -t local/entity_manager .

# Run locally for testing
docker run --rm -it -p 5000:5000 \
  -e HA_URL="http://your-ha-instance:8123" \
  -e HA_TOKEN="your-long-lived-token" \
  local/entity_manager
```

## Contributing

Contributions are welcome! Please read **[CONTRIBUTING.md](CONTRIBUTING.md)** for the
full guide. In short:

1. Fork the repository and branch off **`main`** (`git checkout -b feature/amazing-feature main`)
2. Make your changes; ensure `flake8` / `black` / `isort` / `pytest` pass
3. Use [Conventional Commits](https://www.conventionalcommits.org/) (`feat:`, `fix:`, …)
4. Open a Pull Request **against `main`** — once CI is green it gets squash-merged

We use GitHub Flow: `main` is the only long-lived branch; releases are tags created by
bumping the version in `config.json`.

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

## Acknowledgments

- Built for Home Assistant
- Follows Home Assistant development standards
- Community contributions welcome
