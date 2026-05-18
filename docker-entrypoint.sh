#!/bin/sh
set -e

case "$1" in
    dev|runserver)
        shift
        echo "Iniciando servidor de desarrollo..."
        exec python manage.py runserver 0.0.0.0:8000 "$@"
        ;;

    test)
        shift
        echo "Ejecutando tests..."
        exec python manage.py test "$@"
        ;;

    migrate)
        shift
        exec python manage.py "$@"
        ;;

    daphne|*)
        if [ "$1" = "daphne" ]; then shift; fi

        echo "Aplicando migraciones..."
        python manage.py migrate --noinput

        # Re-run collectstatic at start when source is bind-mounted over /app
        # in dev/staging; in prod the build-time copy at /opt/staticfiles is
        # already populated and this becomes a fast no-op.
        if [ "${JIRRABIT_DEBUG:-0}" = "0" ]; then
            echo "Recolectando estáticos..."
            python manage.py collectstatic --noinput
        fi

        echo "Iniciando Daphne (Producción)..."
        exec daphne -b 0.0.0.0 -p 8000 jirrabit.asgi:application "$@"
        ;;
esac