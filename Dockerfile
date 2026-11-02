FROM python:3.13-slim
WORKDIR /app
RUN mkdir /state /admin /catalog && chown 65532:65532 /state /admin /catalog
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY --chown=65532:65532 artefact /app/artefact
USER 65532:65532
CMD ["python", "-m", "artefact.runtime"]
