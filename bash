export ONEAPI_SERVICE_ID=svc.oneapi-e2
export ONEAPI_SERVICE_PASSWORD=<your-password>
cd "api migration"
python api_oneapi_consumer.py -x getSilenceStatus --job_id <your-job-id>
