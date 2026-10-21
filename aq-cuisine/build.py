# Builds the three AQ Cuisine concepts (symbol, lockup, colour lockup).
A_LIG = "M4 212 L49 44 L79 44 L124 212 L96 212 L84.75 170 L43.25 170 L32 212 Z M64 92.6 L50.2 144 L77.8 144 Z"
def sym_a(g, n):
    return f'''<path fill="{g}" fill-rule="evenodd" d="{A_LIG}"/>
<path fill="{g}" fill-rule="evenodd" d="M168 42 A86 86 0 1 1 167.9 42 Z M168 68 A60 60 0 1 0 168.1 68 Z"/>
<polygon fill="{g}" points="204.5,167.5 252.5,215.5 235.5,232.5 187.5,184.5"/>
<path fill="{n}" d="M134 140 A34 34 0 0 1 202 140 Z"/><rect fill="{n}" x="126" y="144" width="84" height="10" rx="5"/><circle fill="{n}" cx="168" cy="97" r="7"/>'''
def sym_b(g, n):
    return f'''<path fill="{g}" fill-rule="evenodd" d="M20 226 L100 30 L156 30 L236 226 L186 226 L170 186 L86 186 L70 226 Z
M92 146 A36 36 0 0 1 164 146 L172 146 L172 156 L84 156 L84 146 Z M121 95 a7 7 0 1 0 14 0 a7 7 0 1 0 -14 0 Z"/>'''
def sym_c(g, n):
    # dome with A and Q as real holes (evenodd); Q ring and tail drawn as one outline
    return f'''<path fill="{n}" fill-rule="evenodd" d="M24 176 A104 104 0 0 1 232 176 Z
M72 166 L94 102 L110 102 L132 166 L117 166 L110.8 148 L93.2 148 L87 166 Z M102 122.4 L106.7 136 L97.3 136 Z
M194.18 152.4 L205.83 164.05 L198.05 171.83 L186.4 160.18 A32 32 0 1 1 194.18 152.4 Z M168 113 A21 21 0 1 0 168.1 113 Z"/>
<rect fill="{g}" x="8" y="186" width="240" height="20" rx="10"/><circle fill="{n}" cx="128" cy="52" r="12"/>'''
def svg(body, title):
    return f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 256 256"><title>{title}</title>{body}</svg>\n'
def lockup(body, n, word):
    # exploration only: wordmark is live <text>; it gets outlined in the kit
    return (f'<g transform="translate(46 4) scale(0.64)">{body}</g>'
            f'<text x="130" y="214" text-anchor="middle" font-family="Montserrat, DejaVu Sans, sans-serif" '
            f'font-weight="600" font-size="27" letter-spacing="9" fill="{n}">{word}</text>')
GOLD, NAVY = "#B8924F", "#26235F"
for k, f, word in (("a", sym_a, "CUISINE"), ("b", sym_b, "AQ CUISINE"), ("c", sym_c, "CUISINE")):
    t = f"AQ Cuisine concept {k.upper()}"
    open(f"concept-{k}-symbol.svg", "w").write(svg(f("#000", "#000"), t))
    open(f"concept-{k}-lockup.svg", "w").write(svg(lockup(f("#000", "#000"), "#000", word), t + " lockup"))
    open(f"concept-{k}-color.svg", "w").write(svg(lockup(f(GOLD, NAVY), NAVY, word), t + " colour"))
