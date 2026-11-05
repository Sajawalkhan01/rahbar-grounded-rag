# RAHBAR indexer image: the Job's image, and the only writer to Chroma.
# Build context: the REPOSITORY ROOT (it needs corpus/fetch.py and the CI fixtures):
#   docker build -f indexer/Dockerfile -t rahbar-indexer:TAG .
FROM python:3.12.11-slim-bookworm AS build
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN python -m venv /opt/venv
COPY indexer/requirements.txt /tmp/requirements.txt
RUN /opt/venv/bin/pip install -r /tmp/requirements.txt

FROM python:3.12.11-slim-bookworm
ENV PATH=/opt/venv/bin:$PATH PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
# OCR needs system packages, not only pip ones: tesseract for the recognition, poppler for
# rendering PDF pages to images.
RUN apt-get update \
    && apt-get -y upgrade \
    && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-eng poppler-utils \
    && rm -rf /var/lib/apt/lists/*
COPY --from=build /opt/venv /opt/venv
WORKDIR /app
COPY indexer/pipeline /app/pipeline
# The fetch script and its list of public documents: the Job's init container downloads the
# corpus with them (nothing copyrighted is stored in the image or in Git).
COPY corpus/fetch.py corpus/sources.yaml /app/corpus/
# The tiny fixture corpus, so CI can index in a minute (CORPUS_DIR=/fixtures).
COPY eval/fixtures /fixtures
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin rahbar
USER 10001
CMD ["python", "-m", "pipeline.run"]
