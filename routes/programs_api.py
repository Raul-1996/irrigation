"""Programs API blueprint — all /api/programs* endpoints."""
from flask import Blueprint, request, jsonify
import json
import logging

from database import db
from irrigation_scheduler import get_scheduler
from services.api_rate_limiter import rate_limit
import sqlite3

logger = logging.getLogger(__name__)

programs_api_bp = Blueprint('programs_api', __name__)


@programs_api_bp.route('/api/programs')
def api_programs():
    programs = db.get_programs()
    return jsonify(programs)


@programs_api_bp.route('/api/programs/<int:prog_id>', methods=['GET', 'PUT', 'DELETE'])
@rate_limit('programs', max_requests=20, window_sec=60)
def api_program(prog_id):
    if request.method == 'GET':
        program = db.get_program(prog_id)
        return jsonify(program) if program else ('Program not found', 404)

    elif request.method == 'PUT':
        data = request.get_json() or {}
        try:
            if isinstance(data.get('days'), list):
                data['days'] = [int(d) for d in data['days']]
        except (ValueError, TypeError, KeyError) as e:
            logger.debug("Handled exception in api_program: %s", e)
        try:
            conflicts = db.check_program_conflicts(program_id=prog_id, time=data['time'], zones=data['zones'], days=data['days'])
            if conflicts:
                return jsonify({'success': False, 'has_conflicts': True, 'conflicts': conflicts, 'message': 'Обнаружены конфликты программ'})
        except (sqlite3.Error, OSError) as e:
            logger.error(f"Ошибка серверной проверки конфликтов: {e}")
        program = db.update_program(prog_id, data)
        if program:
            db.add_log('prog_edit', json.dumps({"prog": prog_id, "changes": data}))
            try:
                scheduler = get_scheduler()
                if scheduler:
                    scheduler.schedule_program(program['id'], program)
            except (KeyError, TypeError, ValueError) as e:
                logger.error(f"Ошибка перепланирования программы {prog_id}: {e}")
            return jsonify(program)
        return ('Program not found', 404)

    elif request.method == 'DELETE':
        if db.delete_program(prog_id):
            db.add_log('prog_delete', json.dumps({"prog": prog_id}))
            try:
                scheduler = get_scheduler()
                if scheduler:
                    scheduler.cancel_program(prog_id)
            except (ValueError, KeyError, RuntimeError) as e:
                logger.error(f"Ошибка отмены программы {prog_id} в планировщике: {e}")
            return ('', 204)
        return jsonify({'success': False, 'message': 'Program not found'}), 404


@programs_api_bp.route('/api/programs', methods=['POST'])
@rate_limit('programs', max_requests=20, window_sec=60)
def api_create_program():
    data = request.get_json() or {}
    # Validate required fields
    missing = [f for f in ('name', 'time', 'zones') if f not in data]
    if missing:
        return jsonify({'success': False, 'message': f'Missing required fields: {", ".join(missing)}'}), 400
    try:
        if isinstance(data.get('days'), list):
            data['days'] = [int(d) for d in data['days']]
    except (ValueError, TypeError, KeyError) as e:
        logger.debug("Handled exception in api_create_program: %s", e)
    try:
        conflicts = db.check_program_conflicts(program_id=None, time=data['time'], zones=data['zones'], days=data.get('days', []))
        if conflicts:
            return jsonify({'success': False, 'has_conflicts': True, 'conflicts': conflicts, 'message': 'Обнаружены конфликты программ'})
    except (sqlite3.Error, OSError) as e:
        logger.error(f"Ошибка серверной проверки конфликтов (create): {e}")
    program = db.create_program(data)
    if program:
        db.add_log('prog_create', json.dumps({"prog": program['id'], "name": program['name']}))
        try:
            scheduler = get_scheduler()
            if scheduler:
                scheduler.schedule_program(program['id'], program)
        except (KeyError, TypeError, ValueError) as e:
            logger.error(f"Ошибка планирования новой программы {program['id']}: {e}")
        return jsonify(program), 201
    return ('Error creating program', 400)


@programs_api_bp.route('/api/programs/check-conflicts', methods=['POST'])
def check_program_conflicts():
    """Check watering program conflicts."""
    try:
        data = request.get_json()
        program_id = data.get('program_id')
        time_val = data.get('time')
        zones = data.get('zones', [])
        days = data.get('days', [])

        if not time_val or not zones or not days:
            return jsonify({'success': False, 'message': 'Необходимо указать время, дни и зоны'}), 400

        conflicts = db.check_program_conflicts(program_id, time_val, zones, days)
        return jsonify({'success': True, 'conflicts': conflicts, 'has_conflicts': len(conflicts) > 0})
    except (sqlite3.Error, OSError) as e:
        logger.error(f"Ошибка проверки конфликтов программ: {e}")
        return jsonify({'success': False, 'message': 'Ошибка проверки конфликтов'}), 500


@programs_api_bp.route('/api/programs/<int:prog_id>/duplicate', methods=['POST'])
@rate_limit('programs', max_requests=10, window_sec=60)
def api_duplicate_program(prog_id):
    """Duplicate program (create copy with '(копия)' suffix)."""
    try:
        new_program = db.duplicate_program(prog_id)
        if new_program:
            db.add_log('prog_duplicate', json.dumps({"original": prog_id, "copy": new_program['id']}))
            try:
                scheduler = get_scheduler()
                if scheduler:
                    scheduler.schedule_program(new_program['id'], new_program)
            except (KeyError, TypeError, ValueError) as e:
                logger.error(f"Ошибка планирования дубликата программы {new_program['id']}: {e}")
            return jsonify({'success': True, 'program': new_program}), 201
        return jsonify({'success': False, 'message': 'Program not found'}), 404
    except (sqlite3.Error, OSError) as e:
        logger.error(f"Ошибка дублирования программы {prog_id}: {e}")
        return jsonify({'success': False, 'message': 'Ошибка дублирования программы'}), 500


@programs_api_bp.route('/api/programs/<int:prog_id>/enabled', methods=['PATCH'])
@rate_limit('programs', max_requests=20, window_sec=60)
def api_toggle_program_enabled(prog_id):
    """Toggle program enabled/disabled state."""
    try:
        data = request.get_json() or {}
        enabled = data.get('enabled')
        if enabled is None:
            return jsonify({'success': False, 'message': 'enabled field is required'}), 400
        
        program = db.update_program(prog_id, {'enabled': bool(enabled)})
        if program:
            db.add_log('prog_toggle', json.dumps({"prog": prog_id, "enabled": bool(enabled)}))
            try:
                scheduler = get_scheduler()
                if scheduler:
                    if program.get('enabled'):
                        scheduler.schedule_program(prog_id, program)
                    else:
                        scheduler.cancel_program(prog_id)
            except (KeyError, TypeError, ValueError) as e:
                logger.error(f"Ошибка перепланирования программы {prog_id} после toggle: {e}")
            return jsonify({'success': True, 'program': program})
        return jsonify({'success': False, 'message': 'Program not found'}), 404
    except (sqlite3.Error, OSError) as e:
        logger.error(f"Ошибка toggle enabled для программы {prog_id}: {e}")
        return jsonify({'success': False, 'message': 'Ошибка обновления программы'}), 500


@programs_api_bp.route('/api/programs/<int:prog_id>/log', methods=['GET'])
def api_program_log(prog_id):
    """Get watering log for specific program."""
    try:
        program = db.get_program(prog_id)
        if not program:
            return jsonify({'success': False, 'message': 'Program not found'}), 404
        
        period = request.args.get('period', 'today')
        limit = int(request.args.get('limit', 50))
        
        # TODO: implement actual log fetching from zone_runs + logs tables
        # For now return stub
        log_entries = []
        
        return jsonify({'success': True, 'log': log_entries})
    except (ValueError, sqlite3.Error, OSError) as e:
        logger.error(f"Ошибка получения журнала программы {prog_id}: {e}")
        return jsonify({'success': False, 'message': 'Ошибка получения журнала'}), 500


@programs_api_bp.route('/api/programs/<int:prog_id>/stats', methods=['GET'])
def api_program_stats(prog_id):
    """Get statistics for specific program."""
    try:
        program = db.get_program(prog_id)
        if not program:
            return jsonify({'success': False, 'message': 'Program not found'}), 404
        
        # TODO: implement actual stats aggregation from zone_runs table
        # For now return stub
        stats = {
            'total_runs': 0,
            'total_water_calc': 0,
            'total_water_fact': 0,
            'avg_duration_min': 0,
            'last_run': None
        }
        
        return jsonify({'success': True, 'stats': stats})
    except (sqlite3.Error, OSError) as e:
        logger.error(f"Ошибка получения статистики программы {prog_id}: {e}")
        return jsonify({'success': False, 'message': 'Ошибка получения статистики'}), 500
