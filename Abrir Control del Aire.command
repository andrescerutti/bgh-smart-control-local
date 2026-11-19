#!/bin/bash
# Doble click para arrancar el panel de control del aire.
cd "$(dirname "$0")"
echo "Iniciando el panel de control del aire..."
python3 ac_server.py &
SERVER_PID=$!
sleep 1.5
open "http://localhost:8765"
echo ""
echo "El panel ya se abrió en el navegador."
echo "NO CIERRES ESTA VENTANA mientras quieras poder usar el aire desde el panel."
echo "Para cerrar todo, cerrá esta ventana de Terminal."
wait $SERVER_PID
