#!/bin/sh
set -e

case "$1" in
    dev|runserver)
        shift
        echo "Iniciando servidor de desarrollo..."
        exec python manage.py runserver 0.0.0.0:8000
        ;;

    test)
        shift
        echo "Ejecutando tests..."
        exec python manage.py test
        ;;

    migrate)
        shift
        exec python manage.py migrate
        ;;

    saltare)
        shift
        echo "Ejecutando Saltare..."
        exec saltare jirrabit.asgi:application --host 0.0.0.0 --port 8000 --access-log
        ;;

    daphne|*)
        echo "Iniciando Daphne (Producción)..."
        exec daphne -b 0.0.0.0 -p 8000 jirrabit.asgi:application
        ;;
esac