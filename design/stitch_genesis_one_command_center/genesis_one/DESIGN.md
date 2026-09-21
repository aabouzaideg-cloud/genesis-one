---
name: GENESIS-ONE
colors:
  surface: '#131313'
  surface-dim: '#131313'
  surface-bright: '#3a3939'
  surface-container-lowest: '#0e0e0e'
  surface-container-low: '#1c1b1b'
  surface-container: '#201f1f'
  surface-container-high: '#2a2a2a'
  surface-container-highest: '#353534'
  on-surface: '#e5e2e1'
  on-surface-variant: '#e4bebc'
  inverse-surface: '#e5e2e1'
  inverse-on-surface: '#313030'
  outline: '#ab8987'
  outline-variant: '#5b403f'
  surface-tint: '#ffb3b1'
  primary: '#ffb3b1'
  on-primary: '#680011'
  primary-container: '#ff535b'
  on-primary-container: '#5b000e'
  inverse-primary: '#bb152c'
  secondary: '#c1c4e5'
  on-secondary: '#2b2f48'
  secondary-container: '#414560'
  on-secondary-container: '#b0b3d3'
  tertiary: '#ffb3ae'
  on-tertiary: '#68000b'
  tertiary-container: '#ff5352'
  on-tertiary-container: '#5c0008'
  error: '#ffb4ab'
  on-error: '#690005'
  error-container: '#93000a'
  on-error-container: '#ffdad6'
  primary-fixed: '#ffdad8'
  primary-fixed-dim: '#ffb3b1'
  on-primary-fixed: '#410007'
  on-primary-fixed-variant: '#92001c'
  secondary-fixed: '#dee0ff'
  secondary-fixed-dim: '#c1c4e5'
  on-secondary-fixed: '#161a32'
  on-secondary-fixed-variant: '#414560'
  tertiary-fixed: '#ffdad7'
  tertiary-fixed-dim: '#ffb3ae'
  on-tertiary-fixed: '#410004'
  on-tertiary-fixed-variant: '#930014'
  background: '#131313'
  on-background: '#e5e2e1'
  surface-variant: '#353534'
typography:
  display-lg:
    fontFamily: Inter
    fontSize: 32px
    fontWeight: '700'
    lineHeight: '1.2'
    letterSpacing: -0.02em
  header-sm:
    fontFamily: Inter
    fontSize: 14px
    fontWeight: '600'
    lineHeight: '1.4'
    letterSpacing: 0.05em
  body-md:
    fontFamily: Inter
    fontSize: 13px
    fontWeight: '400'
    lineHeight: '1.6'
  data-lg:
    fontFamily: JetBrains Mono
    fontSize: 18px
    fontWeight: '500'
    lineHeight: '1.2'
  data-md:
    fontFamily: JetBrains Mono
    fontSize: 13px
    fontWeight: '400'
    lineHeight: '1.4'
  data-sm:
    fontFamily: JetBrains Mono
    fontSize: 11px
    fontWeight: '400'
    lineHeight: '1.2'
  label-caps:
    fontFamily: JetBrains Mono
    fontSize: 10px
    fontWeight: '700'
    lineHeight: '1'
    letterSpacing: 0.1em
spacing:
  unit: 4px
  gutter: 12px
  margin: 16px
  container-padding: 24px
  grid-size: 32px
---

## Brand & Style
This design system is engineered for high-stakes quantitative analysis, where speed of comprehension and technical precision are paramount. The personality is authoritative, cold, and hyper-functional, evoking the atmosphere of a strategic command center or a high-frequency trading floor. 

The aesthetic leverages **Dark Glassmorphism** layered over a rigid structural grid. It utilizes a "Terminal" philosophy—information density is maximized, and decorative elements are stripped away in favor of functional signals. The emotional response is one of controlled intensity, providing the user with a sense of absolute oversight over volatile global markets. Visual interest is generated through light-emissive properties—specifically low-glow red outlines—rather than traditional depth, simulating a high-end hardware interface.

## Colors
The palette is rooted in **Deep Carbon Black**, which serves as the infinite void for the interface. **Crimson Red** is the primary functional color, used for critical actions, branding, and structural accents. **Neon Red** (#ff002b) is reserved exclusively for active signals, alerts, and real-time data fluctuations that require immediate cognitive load.

**Slate Gray** acts as the utilitarian workhorse, providing legible contrast for secondary data and inactive states without distracting the eye from primary alerts. Transparency is a key component: surfaces should utilize a semi-transparent dark tint to allow the background grid patterns to subtly persist through the UI layers.

## Typography
The typographic strategy differentiates between "Operational UI" and "Technical Data." **Inter** is used for the navigational framework and structural labels to ensure maximum clarity and a modern professional feel.

**JetBrains Mono** is the primary font for all quantitative outputs, price tickers, and terminal logs. The monospaced nature of the font ensures that columns of numbers remain perfectly aligned, facilitating rapid scanning of data tables. Letter-spacing should be slightly increased for all-caps labels to maintain legibility at small sizes.

## Layout & Spacing
This design system employs a **Fixed Grid** philosophy based on a 4px base unit. The background must feature a persistent 32px pixel-grid pattern to anchor elements in space. Layouts are strictly modular; components should "snap" to the grid, creating a sense of engineering precision.

Density is high. Gutters are kept tight (12px) to minimize the distance the eye must travel between data points. Large areas of empty space are discouraged; where data is absent, the background grid pattern should be visible to maintain the "command center" aesthetic.

## Elevation & Depth
Depth is achieved through **Glassmorphism** and light emission rather than traditional drop shadows. 

1.  **Base Layer:** Solid Deep Carbon Black with a faint Slate Gray grid overlay.
2.  **Surface Layer:** Semi-transparent black (75-85% opacity) with a `backdrop-filter: blur(12px)`.
3.  **Edge Definition:** Elements do not cast shadows; instead, they use a 1px solid border. Inactive elements use Slate Gray borders (20% opacity). Active or "Hot" elements use a Crimson Red border with a subtle `box-shadow: 0 0 8px rgba(230, 57, 70, 0.4)` to simulate a low-glow neon effect.
4.  **Z-Index:** Content is strictly flat within its containers; only modal overlays or critical alerts should break the z-plane, using a stronger blur and a more intense red outline.

## Shapes
The shape language is strictly **Sharp (0)**. There are no rounded corners in this design system. Every button, card, input field, and modal must feature 90-degree angles to reinforce the technical, industrial, and "brutalist-light" terminal aesthetic. This maximizes screen real estate and ensures that all elements align perfectly with the background grid.

## Components
- **Buttons:** Rectangular with no radius. Primary buttons are solid Crimson Red with white Inter Medium text. Ghost buttons use a 1px Slate Gray border that turns Neon Red on hover.
- **Data Tables:** High-density JetBrains Mono rows. Alternate rows utilize a 2% white tint for readability. Headers are all-caps Slate Gray with 1px bottom borders.
- **Signals/Chips:** Small, rectangular indicators. "Active" signals use a pulsing Neon Red background; "Stable" signals use a hollow Slate Gray border.
- **Input Fields:** Dark, transparent backgrounds with a bottom-only border. Upon focus, the border glows Crimson Red.
- **Charts:** Vector-based, minimalist lines. Area charts use a Crimson-to-Transparent gradient fill. No grid lines within the chart; use the global background grid for reference.
- **Terminal Logs:** A dedicated component for real-time text streams using JetBrains Mono at 11px, with timestamped entries in Slate Gray and event messages in White or Crimson.
- **Status Hexes:** Minimalist vector icons contained within hexagonal or square frames to indicate system health or market volatility status.