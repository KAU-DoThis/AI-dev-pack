"""화면 겉모습: 어두운 테마 + 보라→청록 포인트. 기능 코드는 이 파일을 몰라도 된다.

Gradio 6 은 theme·css·js 를 launch() 에 넘긴다 (loop/main.py).
늘 어두운 화면으로 열리게 js 로 body 에 dark 를 붙인다 (브라우저가 밝은 모드여도).
"""

import gradio as gr

BG = "#0a0e1a"          # 페이지 배경
PANEL = "#111729"       # 카드
PANEL_2 = "#0d1323"     # 입력칸
LINE = "#222c47"        # 테두리
TEXT = "#e6e9f2"
MUTED = "#8b93a8"
ACCENT = "#8b5cf6"      # 보라
ACCENT_2 = "#06b6d4"    # 청록
GRADIENT = f"linear-gradient(90deg, {ACCENT}, {ACCENT_2})"

THEME = gr.themes.Base(
    primary_hue="violet",
    secondary_hue="cyan",
    neutral_hue="slate",
    radius_size="lg",
    font=[gr.themes.GoogleFont("Noto Sans KR"), "system-ui", "sans-serif"],
    font_mono=[gr.themes.GoogleFont("JetBrains Mono"), "Consolas", "monospace"],
).set(
    body_background_fill_dark=BG,
    body_text_color_dark=TEXT,
    body_text_color_subdued_dark=MUTED,
    background_fill_primary_dark=PANEL,
    background_fill_secondary_dark=PANEL_2,
    border_color_primary_dark=LINE,
    block_background_fill_dark=PANEL,
    block_border_color_dark=LINE,
    block_label_background_fill_dark=PANEL,
    block_label_text_color_dark=MUTED,
    block_title_text_color_dark=TEXT,
    block_info_text_color_dark=MUTED,
    panel_background_fill_dark=PANEL,
    input_background_fill_dark=PANEL_2,
    input_border_color_dark=LINE,
    input_border_color_focus_dark=ACCENT,
    code_background_fill_dark=PANEL_2,
    table_even_background_fill_dark=PANEL,
    table_odd_background_fill_dark=PANEL_2,
    table_border_color_dark=LINE,
    button_primary_background_fill_dark=GRADIENT,
    button_primary_background_fill_hover_dark=f"linear-gradient(90deg, #7c3aed, #0891b2)",
    button_primary_text_color_dark="#ffffff",
    button_primary_border_color_dark="transparent",
    button_secondary_background_fill_dark="#1a2238",
    button_secondary_background_fill_hover_dark="#232d48",
    button_secondary_text_color_dark=TEXT,
    button_secondary_border_color_dark=LINE,
    slider_color_dark=ACCENT,
    checkbox_background_color_selected_dark=ACCENT,
    color_accent_soft_dark="#2a2150",
    block_shadow_dark="0 4px 24px rgba(0, 0, 0, 0.35)",
)

CSS = f"""
.gradio-container {{ max-width: 1280px !important; margin: 0 auto !important; }}
body, .gradio-container {{ background:
    radial-gradient(1200px 500px at 10% -10%, rgba(139, 92, 246, 0.12), transparent 60%),
    radial-gradient(1000px 400px at 100% 0%, rgba(6, 182, 212, 0.08), transparent 60%), {BG} !important; }}

/* 맨 위 제목 */
#app-header {{ padding: 28px 4px 8px; }}
#app-header h1 {{ margin: 0; font-size: 2.1rem; font-weight: 800; letter-spacing: -0.02em;
    background: {GRADIENT}; -webkit-background-clip: text; background-clip: text; color: transparent; }}
#app-header p {{ margin: 6px 0 0; color: {MUTED}; font-size: 0.95rem; }}
#app-header .flow {{ margin-top: 12px; }}
#app-header .flow span {{ color: {MUTED}; font-size: 0.82rem; letter-spacing: 0.04em; }}
#app-header .flow .arrow {{ font-style: normal; margin: 0 10px; color: {ACCENT}; opacity: 0.8; }}

/* 상태줄: 알약 모양 카드 */
#status-bar {{ background: {PANEL}; border: 1px solid {LINE}; border-radius: 999px; padding: 6px 18px !important;
    display: inline-block; width: auto !important; margin: 6px 0 14px; }}
#status-bar p {{ margin: 0; font-size: 0.9rem; }}

/* 탭 */
.tab-wrapper, [role="tablist"] {{ border-bottom: 1px solid {LINE} !important; }}
[role="tab"] {{ font-weight: 600; color: {MUTED} !important; padding: 10px 18px !important; }}
[role="tab"][aria-selected="true"] {{ color: {TEXT} !important; border-bottom: 2px solid {ACCENT} !important; }}

/* 큰 버튼 */
button.primary {{ box-shadow: 0 6px 20px rgba(139, 92, 246, 0.35); font-weight: 700; letter-spacing: 0.01em; }}
button.primary:hover {{ transform: translateY(-1px); }}

/* 카드 간격·제목 */
.block {{ border-radius: 14px !important; }}
.prose h3 {{ font-weight: 700; letter-spacing: -0.01em; }}
footer {{ opacity: 0.5; }}
"""

# 브라우저가 밝은 모드여도 항상 어둡게 연다
FORCE_DARK = """
() => {
  document.body.classList.add('dark');
  document.documentElement.style.colorScheme = 'dark';
}
"""


def header_html(title: str, subtitle: str, steps: list[str]) -> str:
    flow = '<i class="arrow">→</i>'.join(f"<span>{step}</span>" for step in steps)
    return f'<div id="app-header"><h1>{title}</h1><p>{subtitle}</p><p class="flow">{flow}</p></div>'
