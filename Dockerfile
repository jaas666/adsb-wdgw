FROM python:3.12-slim

LABEL maintainer="juan"
LABEL description="ADS-B → WDGWars feeder (no dependencies, stdlib only)"

WORKDIR /app

COPY feeder.py /app/feeder.py

# No pip install needed — pure stdlib
ENTRYPOINT ["python3", "-u", "/app/feeder.py"]
