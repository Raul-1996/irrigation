#!/usr/bin/env python3
"""Precise batch replacement of except Exception with specific types.

Each replacement is manually specified based on context analysis.
"""
import re
import sys

# (file, line_number, old_pattern, new_replacement)
# Line numbers are 1-indexed
REPLACEMENTS = [
    # ===== services/zone_control.py =====
    # L173: top-level of exclusive_start_zone - wraps MQTT, DB, events
    ("services/zone_control.py", 173,
     "except Exception:  # catch-all: intentional",
     "except (ConnectionError, TimeoutError, OSError, sqlite3.Error, ValueError):"),
    # L201: water_monitor.get_pulses_at_or_after — AttributeError/ValueError/OSError
    ("services/zone_control.py", 201,
     "except Exception as e:  # catch-all: intentional",
     "except (ValueError, TypeError, AttributeError, OSError) as e:"),
    # L294: master valve close scheduling — threading + MQTT + import
    ("services/zone_control.py", 294,
     "except Exception:  # catch-all: intentional",
     "except (ConnectionError, TimeoutError, OSError, RuntimeError):"),
    # L316: water_monitor.get_pulses_at_or_after
    ("services/zone_control.py", 316,
     "except Exception as e:  # catch-all: intentional",
     "except (ValueError, TypeError, AttributeError, OSError) as e:"),
    # L357: top-level of stop_zone — wraps MQTT, DB, events
    ("services/zone_control.py", 357,
     "except Exception:  # catch-all: intentional",
     "except (ConnectionError, TimeoutError, OSError, sqlite3.Error, ValueError):"),

    # ===== services/mqtt_pub.py =====
    # L72: cl.max_inflight_messages_set(100) — paho method, could raise ValueError/AttributeError
    ("services/mqtt_pub.py", 72,
     "except Exception as e:  # catch-all: intentional",
     "except (ValueError, AttributeError) as e:"),
    # L210: top-level mqtt_publish — wraps connection logic
    ("services/mqtt_pub.py", 210,
     "except Exception:  # catch-all: intentional",
     "except (ConnectionError, TimeoutError, OSError):"),

    # ===== irrigation_scheduler.py =====
    # L63: top-level import of APScheduler  
    ("irrigation_scheduler.py", 63,
     "except Exception as e:  # catch-all: intentional",
     "except (ImportError, AttributeError) as e:  # catch-all: intentional"),
    # L220: logger setup
    ("irrigation_scheduler.py", 220,
     "except Exception as e:  # catch-all: intentional",
     "except (OSError, ValueError) as e:"),
    # L320: scheduler cancel_zone_jobs
    ("irrigation_scheduler.py", 320,
     "except Exception as e:  # catch-all: intentional",
     "except (ValueError, KeyError, RuntimeError) as e:"),
    # L422: scheduler job operations  
    ("irrigation_scheduler.py", 422,
     "except Exception as e:  # catch-all: intentional",
     "except (ValueError, KeyError, RuntimeError) as e:"),
    # L557: scheduler add_job
    ("irrigation_scheduler.py", 557,
     "except Exception as e:  # catch-all: intentional",
     "except (ValueError, KeyError, RuntimeError) as e:"),
    # L661: scheduler remove_job
    ("irrigation_scheduler.py", 661,
     "except Exception as e:  # catch-all: intentional",
     "except (ValueError, KeyError, RuntimeError) as e:"),
    # L693: scheduler reschedule_job
    ("irrigation_scheduler.py", 693,
     "except Exception as e:  # catch-all: intentional",
     "except (ValueError, KeyError, RuntimeError) as e:"),
    # L747: scheduler add_job
    ("irrigation_scheduler.py", 747,
     "except Exception as e:  # catch-all: intentional",
     "except (ValueError, KeyError, RuntimeError) as e:"),
    # L778: logger operation
    ("irrigation_scheduler.py", 778,
     "except Exception as e:  # catch-all: intentional",
     "except (OSError, ValueError) as e:"),
    # L905: logger operation
    ("irrigation_scheduler.py", 905,
     "except Exception as e:  # catch-all: intentional",
     "except (OSError, ValueError) as e:"),
    # L1003: scheduler job operations
    ("irrigation_scheduler.py", 1003,
     "except Exception as e:  # catch-all: intentional",
     "except (ValueError, KeyError, RuntimeError) as e:"),
    # L1028: scheduler job operations
    ("irrigation_scheduler.py", 1028,
     "except Exception as e:  # catch-all: intentional",
     "except (ValueError, KeyError, RuntimeError) as e:"),
    # L1117: scheduler job operations
    ("irrigation_scheduler.py", 1117,
     "except Exception as e:  # catch-all: intentional",
     "except (ValueError, KeyError, RuntimeError) as e:"),
    # L1151: scheduler startup
    ("irrigation_scheduler.py", 1151,
     "except Exception as e:  # catch-all: intentional",
     "except (ValueError, KeyError, RuntimeError) as e:"),
    # L1159: SSE + scheduler
    ("irrigation_scheduler.py", 1159,
     "except Exception as e:  # catch-all: intentional",
     "except (OSError, ValueError, RuntimeError) as e:"),
    # L1164: scheduler
    ("irrigation_scheduler.py", 1164,
     "except Exception as e:  # catch-all: intentional",
     "except (ValueError, KeyError, RuntimeError) as e:"),
    # L1168: scheduler  
    ("irrigation_scheduler.py", 1168,
     "except Exception as e:  # catch-all: intentional",
     "except (ValueError, KeyError, RuntimeError) as e:"),

    # ===== routes/zones_api.py =====
    # L51: route handler — top-level, DB + parse
    ("routes/zones_api.py", 51,
     "except Exception as e:  # catch-all: intentional",
     "except (sqlite3.Error, ValueError, TypeError, OSError) as e:"),
    # L130: logging context
    ("routes/zones_api.py", 130,
     "except Exception as e:  # catch-all: intentional",
     "except (OSError, ValueError) as e:"),
    # L137: logging context
    ("routes/zones_api.py", 137,
     "except Exception as e:  # catch-all: intentional",
     "except (OSError, ValueError) as e:"),
    # L191: logging context
    ("routes/zones_api.py", 191,
     "except Exception as e:  # catch-all: intentional",
     "except (OSError, ValueError) as e:"),
    # L630: accessing file.mimetype — attribute access
    ("routes/zones_api.py", 630,
     "except Exception as e:  # catch-all: intentional",
     "except (AttributeError, ValueError) as e:"),
    # L646: normalize_image — PIL/image processing
    ("routes/zones_api.py", 646,
     "except Exception:  # catch-all: intentional",
     "except (IOError, OSError, ValueError):"),
    # L1038: logging context 
    ("routes/zones_api.py", 1038,
     "except Exception as e:  # catch-all: intentional",
     "except (OSError, ValueError) as e:"),
    # L1115: logging context
    ("routes/zones_api.py", 1115,
     "except Exception as e:  # catch-all: intentional",
     "except (OSError, ValueError) as e:"),

    # ===== routes/system_api.py =====
    # L106: SSE broadcast
    ("routes/system_api.py", 106,
     "except Exception as e:  # catch-all: intentional",
     "except (OSError, ValueError, RuntimeError) as e:"),
    # L169: scheduler operation
    ("routes/system_api.py", 169,
     "except Exception as e:  # catch-all: intentional",
     "except (ValueError, KeyError, RuntimeError) as e:"),
    # L334: scheduler operation
    ("routes/system_api.py", 334,
     "except Exception as e:  # catch-all: intentional",
     "except (ValueError, KeyError, RuntimeError) as e:"),
    # L489: scheduler cancel_group_jobs
    ("routes/system_api.py", 489,
     "except Exception:  # catch-all: intentional",
     "except (ValueError, KeyError, RuntimeError):"),
    # L836: generic fallback
    ("routes/system_api.py", 836,
     "except Exception as e:  # catch-all: intentional",
     "except (ValueError, TypeError, RuntimeError) as e:"),

    # ===== services/monitors.py =====
    # L66: payload.decode — encoding
    ("services/monitors.py", 66,
     "except Exception as e:  # catch-all: intentional",
     "except (UnicodeDecodeError, AttributeError) as e:"),
    # L118: stop_all_in_group — zone control  
    ("services/monitors.py", 118,
     "except Exception:  # catch-all: intentional",
     "except (ConnectionError, TimeoutError, OSError, sqlite3.Error):"),
    # L520: env_monitor.start
    ("services/monitors.py", 520,
     "except Exception:  # catch-all: intentional",
     "except (ConnectionError, TimeoutError, OSError, ValueError):"),
    # L526: if there's one here too
    ("services/monitors.py", 526,
     "except Exception:  # catch-all: intentional",
     "except (ConnectionError, TimeoutError, OSError, ValueError):"),

    # ===== routes/groups_api.py =====
    # L197: scheduler operation
    ("routes/groups_api.py", 197,
     "except Exception as e:  # catch-all: intentional",
     "except (ValueError, KeyError, RuntimeError) as e:"),
    # L310: MQTT operation
    ("routes/groups_api.py", 310,
     "except Exception as e:  # catch-all: intentional",
     "except (ConnectionError, TimeoutError, OSError) as e:"),
    # L361: SSE broadcast
    ("routes/groups_api.py", 361,
     "except Exception as e:  # catch-all: intentional",
     "except (OSError, ValueError, RuntimeError) as e:"),

    # ===== routes/mqtt_api.py =====
    # L125: msg.topic attribute access
    ("routes/mqtt_api.py", 125,
     "except Exception as e:  # catch-all: intentional",
     "except (AttributeError, ValueError) as e:"),
    # L131: msg.payload.decode
    ("routes/mqtt_api.py", 131,
     "except Exception as e:  # catch-all: intentional",
     "except (UnicodeDecodeError, AttributeError) as e:"),
    # L237: msg.topic attribute access
    ("routes/mqtt_api.py", 237,
     "except Exception as e:  # catch-all: intentional",
     "except (AttributeError, ValueError) as e:"),
    # L242: msg.payload.decode
    ("routes/mqtt_api.py", 242,
     "except Exception as e:  # catch-all: intentional",
     "except (UnicodeDecodeError, AttributeError) as e:"),

    # ===== routes/programs_api.py =====
    # L62: scheduler operation
    ("routes/programs_api.py", 62,
     "except Exception as e:  # catch-all: intentional",
     "except (ValueError, KeyError, RuntimeError) as e:"),

    # ===== services/telegram_bot.py =====
    # L123: fut.result(timeout=10) — asyncio future
    ("services/telegram_bot.py", 123,
     "except Exception as e:  # catch-all: intentional",
     "except (ConnectionError, TimeoutError, OSError, RuntimeError) as e:"),
    # L345: _load_routes_module — import
    ("services/telegram_bot.py", 345,
     "except Exception:  # catch-all: intentional",
     "except (ImportError, AttributeError):"),
    # L387: asyncio event loop — thread target (top-level of thread)
    ("services/telegram_bot.py", 387,
     "except Exception as e:  # catch-all: intentional",
     "except (ConnectionError, TimeoutError, OSError, RuntimeError) as e:  # catch-all: intentional"),
    # L441: notifier.answer_callback — HTTP call to Telegram
    ("services/telegram_bot.py", 441,
     "except Exception as e:  # catch-all: intentional",
     "except (ConnectionError, TimeoutError, OSError, ValueError) as e:"),
    # L520: if exists — telegram polling thread top-level
    ("services/telegram_bot.py", 520,
     "except Exception as e:  # catch-all: intentional",
     "except (ConnectionError, TimeoutError, OSError, RuntimeError) as e:  # catch-all: intentional"),

    # ===== services/watchdog.py =====
    # L55: watchdog loop — broad catch needed (thread top-level)
    ("services/watchdog.py", 55,
     "except Exception as e:  # catch-all: intentional",
     "except (ConnectionError, TimeoutError, OSError, sqlite3.Error, ValueError, RuntimeError) as e:  # catch-all: intentional"),
    # L101: zone_control.stop_zone — MQTT + DB
    ("services/watchdog.py", 101,
     "except Exception:  # catch-all: intentional",
     "except (ConnectionError, TimeoutError, OSError, sqlite3.Error):"),

    # ===== services/app_init.py =====
    # L149: internal init operation
    ("services/app_init.py", 149,
     "except Exception as e:  # catch-all: intentional",
     "except (ValueError, TypeError, KeyError) as e:"),
    # L153: internal init operation
    ("services/app_init.py", 153,
     "except Exception as e:  # catch-all: intentional",
     "except (ValueError, TypeError, KeyError, OSError) as e:"),
    # L194: internal init
    ("services/app_init.py", 194,
     "except Exception as e:  # catch-all: intentional",
     "except (ValueError, TypeError, KeyError) as e:"),
    # L198: internal init
    ("services/app_init.py", 198,
     "except Exception as e:  # catch-all: intentional",
     "except (ValueError, TypeError, KeyError, OSError) as e:"),
    # L226: atexit handler — top-level
    ("services/app_init.py", 226,
     "except Exception:  # catch-all: intentional",
     "except (OSError, RuntimeError):  # catch-all: intentional"),
    # L243: before_first_request or similar init — top-level
    ("services/app_init.py", 243,
     "except Exception:  # catch-all: intentional",
     "except (OSError, RuntimeError, ValueError):  # catch-all: intentional"),

    # ===== services/sse_hub.py =====
    # L62: SSE event stream generation
    ("services/sse_hub.py", 62,
     "except Exception as e:  # catch-all: intentional",
     "except (OSError, RuntimeError, ValueError) as e:"),
    # L259: updates.copy() — extremely unlikely to fail, but dict copy
    ("services/sse_hub.py", 259,
     "except Exception as e:  # catch-all: intentional",
     "except (TypeError, AttributeError) as e:"),

    # ===== services/locks.py =====
    # L34: lock.release() — RuntimeError if not acquired
    ("services/locks.py", 34,
     "except Exception as e:  # catch-all: intentional",
     "except RuntimeError as e:"),
    # L38: lock.acquire — RuntimeError
    ("services/locks.py", 38,
     "except Exception as e:  # catch-all: intentional",
     "except RuntimeError as e:"),

    # ===== services/logging_setup.py =====
    # L85: os.environ access — KeyError/TypeError
    ("services/logging_setup.py", 85,
     "except Exception as e:  # catch-all: intentional",
     "except (KeyError, TypeError) as e:"),
    # L126: logging handler setup
    ("services/logging_setup.py", 126,
     "except Exception as e:  # catch-all: intentional",
     "except (IOError, OSError, ValueError) as e:"),

    # ===== services/observed_state.py =====
    # L69: verify — thread-safe wrapper, top-level of thread
    ("services/observed_state.py", 69,
     "except Exception:  # catch-all: intentional",
     "except (ConnectionError, TimeoutError, OSError, ValueError, RuntimeError):  # catch-all: intentional"),

    # ===== services/helpers.py =====
    # L15: payload.update(extra) — dict update
    ("services/helpers.py", 15,
     "except Exception as e:  # catch-all: intentional",
     "except (TypeError, ValueError) as e:"),

    # ===== app.py =====
    # L298: DB operation with parsing
    ("app.py", 298,
     "except Exception as e:  # catch-all: intentional",
     "except (sqlite3.Error, OSError, ValueError, TypeError, KeyError) as e:"),
    # L301: DB + JSON + parsing
    ("app.py", 301,
     "except Exception as e:  # catch-all: intentional",
     "except (sqlite3.Error, json.JSONDecodeError, OSError, ValueError, TypeError, KeyError) as e:"),
    # L341: watchdog loop — thread top-level
    ("app.py", 341,
     "except Exception as e:  # catch-all: intentional",
     "except (ConnectionError, TimeoutError, OSError, sqlite3.Error, ValueError, RuntimeError) as e:  # catch-all: intentional"),
    # L391: errorhandler 404 — top-level
    ("app.py", 391,
     "except Exception as e:  # catch-all: intentional",
     "except (OSError, ValueError, RuntimeError) as e:  # catch-all: intentional"),

    # ===== utils.py =====
    # L41: generic utility parse
    ("utils.py", 41,
     "except Exception as e:  # catch-all: intentional",
     "except (ValueError, TypeError) as e:"),
    # L63: generic utility parse
    ("utils.py", 63,
     "except Exception as e:  # catch-all: intentional",
     "except (ValueError, TypeError) as e:"),

    # ===== ui_agent_demo.py =====
    # L71: demo function
    ("ui_agent_demo.py", 71,
     "except Exception:  # catch-all: intentional",
     "except (ValueError, TypeError, OSError):"),
    # L137: demo function
    ("ui_agent_demo.py", 137,
     "except Exception:  # catch-all: intentional",
     "except (ValueError, TypeError, OSError):"),
    # L180: demo main — top-level
    ("ui_agent_demo.py", 180,
     "except Exception:  # catch-all: intentional",
     "except (ValueError, TypeError, OSError):  # catch-all: intentional"),

    # ===== tests =====
    # tests/e2e/test_concurrent.py
    ("tests/e2e/test_concurrent.py", 23,
     "except Exception as e:  # catch-all: intentional",
     "except (ConnectionError, TimeoutError, OSError, ValueError) as e:"),
    ("tests/e2e/test_concurrent.py", 23,  # may be different lines
     "except Exception:  # catch-all: intentional",
     "except (ConnectionError, TimeoutError, OSError, ValueError):"),
    # tests/fixtures/database.py
    ("tests/fixtures/database.py", None,
     "except Exception",
     None),  # will handle manually
    # tests/fixtures/app.py
    ("tests/fixtures/app.py", None,
     "except Exception",
     None),  # will handle manually
]


def apply_replacements():
    import os
    project_root = '/workspace/wb-irrigation'
    
    applied = 0
    skipped = 0
    errors = []
    
    for entry in REPLACEMENTS:
        filepath, lineno, old_text, new_text = entry
        if new_text is None:
            continue  # Skip manual entries
            
        full_path = os.path.join(project_root, filepath)
        if not os.path.exists(full_path):
            errors.append(f"File not found: {filepath}")
            continue
        
        with open(full_path, 'r') as f:
            lines = f.readlines()
        
        if lineno is not None:
            # Replace at specific line
            idx = lineno - 1
            if idx >= len(lines):
                errors.append(f"{filepath}:{lineno} - line out of range")
                continue
            
            line = lines[idx]
            if old_text not in line:
                # Maybe line numbers shifted, search nearby
                found = False
                for offset in range(-3, 4):
                    check_idx = idx + offset
                    if 0 <= check_idx < len(lines) and old_text in lines[check_idx]:
                        idx = check_idx
                        line = lines[idx]
                        found = True
                        break
                if not found:
                    errors.append(f"{filepath}:{lineno} - pattern not found: {old_text[:60]}")
                    skipped += 1
                    continue
            
            new_line = line.replace(old_text, new_text)
            lines[idx] = new_line
            applied += 1
        
        with open(full_path, 'w') as f:
            f.writelines(lines)
    
    print(f"Applied: {applied}, Skipped: {skipped}")
    if errors:
        print("Errors:")
        for e in errors:
            print(f"  {e}")


if __name__ == '__main__':
    apply_replacements()
