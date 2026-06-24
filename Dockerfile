FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        curl gnupg tor ca-certificates libxml2-dev libxslt1-dev gcc procps && \
    curl -s https://ngrok-agent.s3.amazonaws.com/ngrok.asc \
        | gpg --dearmor -o /etc/apt/trusted.gpg.d/ngrok.gpg && \
    echo "deb https://ngrok-agent.s3.amazonaws.com buster main" \
        > /etc/apt/sources.list.d/ngrok.list && \
    apt-get update && apt-get install -y --no-install-recommends ngrok && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt
COPY . /app

# Wlasny torrc -- DataDirectory w /tmp, ControlPort 9051
RUN cp /app/torrc /etc/tor/torrc
RUN chmod +x /app/start.sh

EXPOSE 8000

CMD ["/app/start.sh"]
