"""Tests for desktop sidebar layout in status.html."""
import pytest


@pytest.mark.xfail(reason="Implementation pending")
def test_status_html_has_desktop_layout(client):
    """Verify status.html contains desktop-layout wrapper and sidebar elements."""
    response = client.get('/status')
    assert response.status_code == 200
    html = response.data.decode('utf-8')
    
    # Check for main layout structure
    assert 'class="desktop-layout"' in html
    assert 'class="weather-sidebar"' in html
    assert 'id="weather-sidebar"' in html
    assert 'class="main-content"' in html
    assert 'class="sidebar-toggle"' in html
    assert 'id="sidebar-toggle"' in html


@pytest.mark.xfail(reason="Implementation pending")
def test_status_html_has_active_zone_indicator(client):
    """Verify status.html contains active zone indicator in sidebar."""
    response = client.get('/status')
    assert response.status_code == 200
    html = response.data.decode('utf-8')
    
    assert 'id="sidebar-active-zone"' in html
    assert 'active-zone-header' in html
    assert 'id="active-zone-name"' in html
    assert 'id="active-zone-timer"' in html
    assert 'id="active-zone-progress"' in html
    assert 'id="active-zone-next"' in html


@pytest.mark.xfail(reason="Implementation pending")
def test_status_html_has_water_meter(client):
    """Verify status.html contains water meter widget in sidebar."""
    response = client.get('/status')
    assert response.status_code == 200
    html = response.data.decode('utf-8')
    
    assert 'id="sidebar-water-meter"' in html
    assert 'water-meter-header' in html
    assert 'id="water-meter-value"' in html
    assert 'id="water-meter-detail"' in html


def test_weather_widget_in_sidebar(client):
    """Verify weather-widget is inside weather-sidebar."""
    response = client.get('/status')
    assert response.status_code == 200
    html = response.data.decode('utf-8')
    
    # Find positions in HTML
    sidebar_start = html.find('class="weather-sidebar"')
    sidebar_end = html.find('</aside>', sidebar_start)
    weather_widget_pos = html.find('id="weather-widget"')
    
    assert sidebar_start != -1, "weather-sidebar not found"
    assert weather_widget_pos != -1, "weather-widget not found"
    assert sidebar_start < weather_widget_pos < sidebar_end, "weather-widget not inside weather-sidebar"


@pytest.mark.xfail(reason="Implementation pending")
def test_24h_grid_exists(client):
    """Verify CSS contains weather-24h-grid styles."""
    response = client.get('/status')
    assert response.status_code == 200
    html = response.data.decode('utf-8')
    
    assert 'weather-24h-grid' in html
    # Check CSS definition
    assert 'grid-template-columns: repeat(3, 1fr)' in html or 'grid-template-columns:repeat(3,1fr)' in html


@pytest.mark.xfail(reason="Implementation pending")
def test_sidebar_collapsed_css(client):
    """Verify CSS contains sidebar-collapsed styles."""
    response = client.get('/status')
    assert response.status_code == 200
    html = response.data.decode('utf-8')
    
    assert 'sidebar-collapsed' in html


@pytest.mark.xfail(reason="Implementation pending")
def test_mobile_media_query(client):
    """Verify mobile responsive media query exists."""
    response = client.get('/status')
    assert response.status_code == 200
    html = response.data.decode('utf-8')
    
    assert '@media (max-width: 1023px)' in html or '@media(max-width:1023px)' in html


def test_mobile_zones_cards_class(client):
    """Verify HTML contains zone list container for mobile view (v2: Hunter-style)."""
    response = client.get('/status')
    assert response.status_code == 200
    html = response.data.decode('utf-8')
    
    # v2: zone-list replaces zones-cards
    assert 'id="zoneList"' in html or 'id="zones-cards"' in html


def test_mobile_buttons_responsive(client):
    """Verify CSS contains media query for responsive layout."""
    response = client.get('/status')
    assert response.status_code == 200
    html = response.data.decode('utf-8')
    
    # v2: breakpoint raised to 1023px
    assert '@media (max-width: 1023px)' in html or '@media(max-width:1023px)' in html or '@media (max-width: 767px)' in html
