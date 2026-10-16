"""Isolated browser-test server with explicitly synthetic LSL, never human data."""
import json
import sys
import time
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'src'),str(ROOT.parent/'ningsi/src')]
from ningsi_studio.acquisition.sim_outlet import SimulatedEegOutlet
from ningsi_studio.app import create_app
from ningsi_studio.settings import Settings
from ningsi_studio.http.server import AppServer
from ningsi_studio.http.router import Response

out=ROOT.parent.parent/'verification/2026-10-16/neurofocus'
out.mkdir(parents=True,exist_ok=True)
outlet=SimulatedEegOutlet(name='nf-test-sim-eeg',channels=2,srate=250,state_seconds=20)
outlet.start()
app=create_app(Settings(data_dir=out/'server-data',host='127.0.0.1',port=8796,port_scan=1))

@app.server.router.post(r'/api/__test__/outlet/stop')
def stop_outlet(request):
    outlet.stop()
    return Response.json({'synthetic_outlet':'stopped'})

@app.server.router.post(r'/api/__test__/outlet/start')
def start_outlet(request):
    outlet.start()
    return Response.json({'synthetic_outlet':'started'})

server=app.server
try:
    print('Synthetic LSL fixture http://127.0.0.1:8796',flush=True)
    server.serve_forever()
finally:
    outlet.stop()
    app.manager.shutdown()

