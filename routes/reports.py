from flask import Blueprint, jsonify, request
from services.reports import build_report_text

reports_bp = Blueprint('reports_bp', __name__)

@reports_bp.route('/api/reports')
def api_reports():
    period = request.args.get('period','today')
    fmt = request.args.get('format','brief')
    txt = build_report_text(period=period, fmt='brief' if fmt!='full' else 'full')
    return jsonify({'success': True, 'text': txt})

