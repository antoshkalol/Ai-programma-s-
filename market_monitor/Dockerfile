ARG BUILD_FROM
FROM ${BUILD_FROM}

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Alle afhankelijkheden hebben kant-en-klare wheels voor aarch64/musl: niets hoeft gecompileerd te worden
COPY app/requirements.txt ./
RUN pip3 install --only-binary=:all: -r requirements.txt

COPY app/market_monitor ./market_monitor
COPY run.sh /run.sh
RUN chmod a+x /run.sh

CMD [ "/run.sh" ]
