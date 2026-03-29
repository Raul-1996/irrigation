"""Programs API blueprint — all /api/programs* endpoints."""
from flask import Blueprint, request, jsonify
import json
import logging

from database import db
from irrigation_scheduler import get_scheduler
from services.helpers import api_error
from services.api_rate_limiter import rate_limit

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
        except Exception as e:
            logger.debug("Handled exception in api_program: %s", e)
        try:
            conflicts = db.check_program_conflicts(program_id=prog_id, time=data['time'], zones=data['zones'], days=data['days'])
            if conflicts:
                return jsonify({'success': False, 'has_conflicts': True, 'conflicts': conflicts, 'message': 'Обнаружены конфликты программ'})
        except Exception as e:
            logger.error(f"Ошибка серверной проверки конфликтов: {e}")
        program = db.update_program(prog_id, data)
        if program:
            db.add_log('prog_edit', json.dumps({"prog": prog_id, "changes": data}))
            try:
                scheduler = get_scheduler()
                if scheduler:
                    scheduler.schedule_program(program['id'], program)
            except Exception as e:
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
            except Exception as e:
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
    except Exception as e:
        logger.debug("Handled exception in api_create_program: %s", e)
    try:
        conflicts = db.check_program_conflicts(program_id=None, time=data['time'], zones=data['zones'], days=data.get('days', []))
        if conflicts:
            return jsonify({'success': False, 'has_conflicts': True, 'conflicts': conflicts, 'message': 'Обнаружены конфликты программ'})
    except Exception as e:
        logger.error(f"Ошибка серверной проверки конфликтов (create): {e}")
    program = db.create_program(data)
    if program:
        db.add_log('prog_create', json.dumps({"prog": program['id'], "name": program['name']}))
        try:
            scheduler = get_scheduler()
            if scheduler:
                scheduler.schedule_program(program['id'], program)
        except Exception as e:
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
    except Exception as e:
        logger.error(f"Ошибка проверки конфликтов программ: {e}")
        return jsonify({'success': False, 'message': 'Ошибка проверки конфликтов'}), 500
