#!/usr/bin/with-contenv bashio

# Configure logging
export LOG_LEVEL=$(bashio::config 'log_level' || echo 'info')
export ENABLE_DISABLED_ENTITIES=$(bashio::config 'enable_disabled_entities' || echo 'false')
export ENABLE_Z2M_BRIDGE=$(bashio::config 'enable_z2m_bridge' || echo 'true')
export Z2M_BASE_TOPIC=$(bashio::config 'z2m_base_topic' || echo 'zigbee2mqtt')

bashio::log.info "Starting Entity Manager..."
bashio::log.warning "⚠️ BETA VERSION - This add-on is under active development!"
bashio::log.warning "⚠️ Use with caution - Test before relying on it in production!"

# Set environment variables
export HA_URL="http://supervisor/core"
export HA_TOKEN="${SUPERVISOR_TOKEN}"

bashio::log.info "Environment setup complete"
bashio::log.info "HA_URL: ${HA_URL}"
bashio::log.info "LOG_LEVEL: ${LOG_LEVEL}"
bashio::log.info "ENABLE_DISABLED_ENTITIES: ${ENABLE_DISABLED_ENTITIES}"

# Check if web_ui.py exists
if [ -f /app/web_ui.py ]; then
    bashio::log.info "Found web_ui.py at /app/web_ui.py"
else
    bashio::log.error "web_ui.py not found at /app/web_ui.py!"
    ls -la /app/
fi

# Start the Flask application
bashio::log.info "Starting Flask application..."
cd /app
python3 -u web_ui.py 2>&1 | while IFS= read -r line; do
    bashio::log.info "Flask: ${line}"
done
