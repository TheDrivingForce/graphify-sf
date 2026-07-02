uv run graphify-sfdx extract C:\Development\EventSpark\eventspark-labs\eventspark\document-templates 

uv run graphify-sfdx extract C:\Development\EventSpark\eventspark\eventspark\microsites --no-ooe --no-field --no-same-class-calls

python -m graphify cluster-only

python -m graphify query "list the isolated nodes"  