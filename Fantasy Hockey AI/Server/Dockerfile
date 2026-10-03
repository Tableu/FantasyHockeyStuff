# The phone-app server's Linux environment (Fantasy Hockey Phone App Plan, Phase 1): Python 3.13
# (the PC's version), Microsoft ODBC Driver 18 (the driver both database readers name) and the
# packages the plan steps import. The code and its data are not copied in: compose.yaml mounts the
# repo at /app, so an edit on Windows runs in the container at once. Azure gets an image with the
# code baked in later (Phase 4).
FROM python:3.13-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# ODBC Driver 18 from Microsoft's Debian 12 repository; libgomp1 for LightGBM; tzdata for the
# zoneinfo time zones (Fleaflicker's Eastern week boundaries, TZ in compose.yaml).
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates gnupg libgomp1 tzdata \
 && curl -fsSL https://packages.microsoft.com/config/debian/12/packages-microsoft-prod.deb -o /tmp/ms.deb \
 && dpkg -i /tmp/ms.deb && rm /tmp/ms.deb \
 && apt-get update \
 && ACCEPT_EULA=Y apt-get install -y --no-install-recommends msodbcsql18 unixodbc \
 && apt-get purge -y gnupg && apt-get autoremove -y \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /tmp/requirements.txt
RUN pip install -r /tmp/requirements.txt

WORKDIR /app
