"""Save all bytes advertised by MEMOry; keep trigger settings and restore prior RUN."""
import argparse
from dataclasses import asdict
from datetime import datetime
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from zds_scope.driver import ZDScopeDriver
from zds_scope.records import digest, save_json


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--ip", default="192.168.1.100")
    parser.add_argument("--output", default="data/device_validation")
    args = parser.parse_args()
    root = Path(args.output) / datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    root.mkdir(parents=True)
    driver = ZDScopeDriver(args.ip, timeout=10)
    previous = None
    wfm = None
    last_progress = 0

    def progress(received, total, speed, elapsed, eta):
        nonlocal last_progress
        if time.monotonic() - last_progress >= 2 or received == total:
            print(f"TRANSFER {received}/{total} bytes {speed:.2f} MB/s elapsed={elapsed:.1f}s", flush=True)
            last_progress = time.monotonic()

    try:
        driver.connect()
        config = driver.get_scope_actual_configuration()
        previous = config["run_state"]
        save_json(root / "configuration_before.json", config)
        print("CURRENT_TRIGGER", config["extended_settings"].get(":TRIGger:EDGE:SOURce"), flush=True)
        driver.stop_and_wait()
        total, wfm = driver.read_waveform_to_file(config["enabled_channels"], str(root / "waveform.wfm"), progress_callback=progress)
        source = driver.query(":TRIGger:EDGE:SOURce?") if config["extended_settings"].get(":TRIGger:MODE", "").upper() == "EDGE" else None
        result = {"advertised_bytes": total, "saved_bytes": (root / "waveform.wfm").stat().st_size,
                  "sha256": digest(root / "waveform.wfm"), "header": asdict(wfm.head),
                  "channels": {str(ch): {"raw_code_bytes": len(data.raw_data), "item": asdict(data.info)}
                               for ch, data in wfm.channels.items()},
                  "trigger_source_after": source, "encoding_calibration": "unverified"}
        save_json(root / "transfer_result.json", result)
        print("RESULT", json.dumps(result), flush=True)
    except Exception as error:
        save_json(root / "failure.json", {"error": str(error)})
        print("VALIDATION_FAILED", str(error), flush=True)
    finally:
        if wfm:
            wfm.close()
        if previous and previous.lower() == "run" and driver.is_connected:
            driver.run()
            print("RUN_RESTORED", flush=True)
        driver.disconnect()
        print("OUTPUT", root.resolve(), flush=True)


if __name__ == "__main__":
    main()
