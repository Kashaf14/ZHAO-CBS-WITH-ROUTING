FROM python:3.14.2-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MPLBACKEND=Agg \
    MPLCONFIGDIR=/tmp/matplotlib \
    TSN_LIBRARY_PATH=/opt/hamza

COPY containers/cbs/requirements.txt /app/requirements.txt

RUN python -m pip install --no-cache-dir -r requirements.txt

COPY optimizer-manager/cbs_service.py /app/cbs_service.py
COPY optimizer-manager/cbs_response_adapter.py /app/cbs_response_adapter.py
COPY optimizer-manager/usecase_adapter.py /app/usecase_adapter.py
COPY optimizer-manager/cbs_precheck.py /app/cbs_precheck.py
COPY optimizer-manager/cbs_route_response.py /app/cbs_route_response.py


COPY optimizer-manager/optimizers/base.py /app/optimizers/base.py
COPY optimizer-manager/optimizers/cbs.py /app/optimizers/cbs.py
COPY optimizer-manager/optimizers/topology.py /app/optimizers/topology.py

# Generic N-class CBS implementation
COPY optimizer-manager/cbs_route_preparation.py /app/cbs_route_preparation.py
COPY optimizer-manager/optimizers/cbs_nclass_candidate.py /app/optimizers/cbs_nclass_candidate.py
COPY optimizer-manager/optimizers/cbs_multiclass_math.py /app/optimizers/cbs_multiclass_math.py
COPY optimizer-manager/optimizers/cbs_priority_dependencies.py /app/optimizers/cbs_priority_dependencies.py
COPY optimizer-manager/cbs_translation_boundary.py /app/cbs_translation_boundary.py
COPY optimizer-manager/cbs_class_translation.py /app/cbs_class_translation.py

RUN touch /app/optimizers/__init__.py

EXPOSE 8001

CMD ["python", "-m", "uvicorn", "cbs_service:app", "--host", "0.0.0.0", "--port", "8001"]