FROM apify/actor-python:3.12
COPY requirements-actor.txt ./
RUN pip install --no-cache-dir -r requirements-actor.txt
COPY eidas_check ./eidas_check
# Bake the EU trusted lists (signatures verified by pyHanko) into the image; a run refreshes them live if > 7 days old.
RUN python3 -m eidas_check.tl build
CMD ["python3", "-m", "eidas_check.actor"]
