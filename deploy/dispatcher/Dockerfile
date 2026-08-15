# The resident AI dispatcher — vo-merge's fixer as a supervised container.
# See dispatcher.py for the protocol; see README.md for the compose/Unraid wiring.
FROM node:22-slim

RUN npm install -g @anthropic-ai/claude-code \
    && apt-get update \
    && apt-get install -y --no-install-recommends python3 ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

COPY dispatcher.py /dispatcher.py

ENV TICKETS=/tickets

# Dead-by-platform-standards: the loop touches .heartbeat every cycle (POLL_S=30 by default),
# so 3 missed minutes means the process is wedged — restart it.
HEALTHCHECK --interval=60s --timeout=5s --start-period=30s \
  CMD python3 -c "import os,sys,time; p=os.environ.get('TICKETS','/tickets')+'/.heartbeat'; \
sys.exit(0 if os.path.exists(p) and time.time()-os.path.getmtime(p) < 180 else 1)"

CMD ["python3", "/dispatcher.py"]
