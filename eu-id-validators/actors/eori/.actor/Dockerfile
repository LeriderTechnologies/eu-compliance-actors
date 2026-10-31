FROM apify/actor-python:3.12
COPY requirements-actor.txt ./
RUN pip install --no-cache-dir -r requirements-actor.txt
COPY eu_ids ./eu_ids
CMD ["python3", "-m", "eu_ids.eori_actor"]
