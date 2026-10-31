FROM apify/actor-python:3.12
COPY requirements-actor.txt ./
RUN pip install --no-cache-dir -r requirements-actor.txt
COPY einvoice ./einvoice
# Download the official rule sets (pinned versions, SHA-256 checked), compile Peppol's Schematron, then compile every
# rule set once so a broken rule bundle fails the build instead of a customer's run.
RUN python3 -m einvoice.rules build && python3 -c "from einvoice import core, rules; e = core.Engine(); e.run(e.warm, tuple(rules.XSLT)).result(); print('rules OK', e.manifest)"
CMD ["python3", "-m", "einvoice.actor"]
