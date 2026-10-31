FROM apify/actor-python:3.12
COPY requirements-actor.txt ./
RUN pip install --no-cache-dir -r requirements-actor.txt
COPY licence_check ./licence_check
# Lookup index built from the recorder snapshots (python3 -m licence_check.index build), refreshed live when stale
COPY data/index.json.gz ./data/index.json.gz
# LEI snapshot (GLEIF, CC0): register LEIs + all KVK-registered NL LEIs + 90-day changes (python3 -m licence_check.lei build)
COPY data/lei.tsv.gz ./data/lei.tsv.gz
CMD ["python3", "-m", "licence_check.actor"]
