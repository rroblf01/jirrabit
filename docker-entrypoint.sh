#!/bin/sh
set -e

# Definimos la acción basada en el primer argumento
case "$1" in
    # Si el argumento es "dev" o "runserver"
    dev|runserver)
        shift # Eliminamos "dev" de los argumentos para no pasarlo a python
        echo "Iniciando servidor de desarrollo..."
        exec python manage.py runserver 0.0.0.0:8000 "$@"
        ;;

    # Si el argumento es "test"
    test)
        shift
        echo "Ejecutando tests..."
        exec python manage.py test "$@"
        ;;

    # Por defecto (si no hay argumentos o es "daphne")
    daphne|*)
        # Si el primer argumento era "daphne", lo quitamos. 
        # Si era otra cosa (o nada), lo mantenemos como parte de los comandos de daphne.
        if [ "$1" = "daphne" ]; then shift; fi
        
        echo "Iniciando Daphne (Producción)..."
        exec daphne -b 0.0.0.0 -p 8000 jirrabit.asgi:application "$@"
        ;;
esac