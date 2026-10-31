FROM apify/actor-python:3.12
COPY requirements-actor.txt ./
RUN pip install --no-cache-dir -r requirements-actor.txt
COPY eu_common ./eu_common
COPY vat_validator ./vat_validator
CMD ["python3", "-m", "vat_validator.actor"]
