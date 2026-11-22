
FROM python:3.14.2-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MPLBACKEND=Agg \
    MPLCONFIGDIR=/tmp/matplotlib \
    TSN_LIBRARY_PATH=/opt/hamza

# Install CBS dependencies
COPY requirements.txt .
RUN python -m pip install --no-cache-dir -r requirements.txt

# CBS HTTP service and adapters
COPY cbs_service.py \
     usecase_adapter.py \
     cbs_response_adapter.py \
     cbs_precheck.py \
     cbs_route_preparation.py \
     cbs_route_response.py \
     cbs_translation_boundary.py \
     cbs_class_translation.py \
     ./

# Central optimizer identity and version
COPY optimizer_metadata.py ./

# CBS algorithm package
COPY algorithm/ ./algorithm/

EXPOSE 8001

CMD ["python", "-m", "uvicorn", "cbs_service:app", "--host", "0.0.0.0", "--port", "8001"]