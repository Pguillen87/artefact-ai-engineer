FROM python:3.13-slim@sha256:ae14d4ce8771606477a81446e7ef429edd6762305fc50c84507c1041fa8285d1
WORKDIR /app
RUN mkdir /state /admin /catalog /config /secrets && chown 65532:65532 /state /admin /catalog /config /secrets
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY --chown=65532:65532 artefact /app/artefact
USER 65532:65532
CMD ["python", "-m", "artefact.chat_api"]
