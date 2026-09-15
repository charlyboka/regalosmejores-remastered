release: python manage.py migrate --noinput
web: gunicorn config.wsgi --workers 2 --threads 4 --timeout 60 --access-logfile - --error-logfile -
worker: python manage.py run_worker
