/**
 * The color palettes Settings > Appearance offers. Each palette has a light
 * and a dark mode; System still picks the mode. paletteTokens.ts derives
 * every theme token from these seeds.
 *
 * Night comfort rules every palette keeps (pinned in palettes.test.ts):
 * no pure black or pure white canvas, no pure white text, and dark text
 * contrast capped so bright letters do not glow on a dark page.
 *
 * Named palettes follow the published colors of the open-source themes they
 * are named after, softened where the rules above ask for it.
 */
import type { Palette } from './paletteTokens';

/** The palette themes.css already holds. It needs no injected stylesheet. */
export const DEFAULT_PALETTE_ID = 'indigo';

export const PALETTES: readonly Palette[] = [
  {
    id: 'indigo',
    name: 'Soft Indigo',
    note: 'The default: violet and blue on a soft indigo canvas.',
    tone: 'cool',
    dark: {
      bg: '#14132a', text: '#c9c8e0', heading: '#f1f0ff', muted: '#8c8bab',
      accent: '#9a82ff', accent2: '#7cb7f0',
      red: '#ff7a7a', green: '#7fd3a0', amber: '#f3b54a', blue: '#7cb7f0',
      violet: '#b49cff', pink: '#f28cbc', orange: '#f5a25d',
    },
    light: {
      bg: '#eef0f7', text: '#46445e', heading: '#1c1a4a', muted: '#64637f',
      accent: '#6a45e8', accent2: '#3f7fd9',
      red: '#c23a40', green: '#24774d', amber: '#8f5600', blue: '#2a6fa8',
      violet: '#6a45e8', pink: '#a8326a', orange: '#a8510a',
    },
  },
  {
    id: 'harbor',
    name: 'Harbor',
    note: 'Deep navy with sky blue and a warm amber.',
    tone: 'cool',
    dark: {
      bg: '#0f1e2a', text: '#c6d5df', heading: '#e9f1f6', muted: '#8aa2b1',
      accent: '#5cb8d6', accent2: '#ffb703',
      red: '#ff8a80', green: '#a6d98c', amber: '#ffb703', blue: '#8ecae6',
      violet: '#c9a7ff', pink: '#f2a0c4', orange: '#fb8500',
    },
    light: {
      bg: '#e9eff3', text: '#23394a', heading: '#023047', muted: '#526a78',
      accent: '#166e8c', accent2: '#a84f00',
      red: '#c0262d', green: '#2f7d32', amber: '#9a5b00', blue: '#1b6fa8',
      violet: '#7b4bb5', pink: '#a3366a', orange: '#a84f00', paper: '#f7fafc',
    },
  },
  {
    id: 'doku',
    name: 'Doku Mint',
    note: 'Graphite and paper gray with a fresh mint green.',
    tone: 'neutral',
    dark: {
      bg: '#151515', text: '#d0d0d0', heading: '#ececec', muted: '#8a8a8a',
      accent: '#3dd68c', accent2: '#818cf8',
      red: '#f87171', green: '#3dd68c', amber: '#ffb74c', blue: '#818cf8',
      violet: '#c792ea', pink: '#f0a0c8', orange: '#f59e5b',
    },
    light: {
      bg: '#efefec', text: '#2a2a2a', heading: '#141414', muted: '#666666',
      accent: '#0f7a4a', accent2: '#4f46e5',
      red: '#cf222e', green: '#0f7a4a', amber: '#8f5a00', blue: '#4f46e5',
      violet: '#7c3aed', pink: '#b0306a', orange: '#b45309', paper: '#fbfbf9',
    },
  },
  {
    id: 'lagoon',
    name: 'Lagoon',
    note: 'Dark teal and sand with terracotta highlights.',
    tone: 'warm',
    dark: {
      bg: '#0c1d23', text: '#cbdad4', heading: '#eef1e6', muted: '#8ea39f',
      accent: '#3fb8b0', accent2: '#ee9b00',
      red: '#e0705c', green: '#94d2bd', amber: '#ee9b00', blue: '#6cb4d4',
      violet: '#b4a2e0', pink: '#e48f9e', orange: '#e08a3a',
    },
    light: {
      bg: '#f0eadb', text: '#213a3f', heading: '#001219', muted: '#5e6e6c',
      accent: '#005f73', accent2: '#bb3e03',
      red: '#ae2012', green: '#2f6f4f', amber: '#8a5a00', blue: '#1f5f8a',
      violet: '#5f4a99', pink: '#9b2257', orange: '#bb3e03', paper: '#fbf7ee',
    },
  },
  {
    id: 'solarized',
    name: 'Solarized',
    note: 'The classic low-glare blue and beige, made for long sessions.',
    tone: 'warm',
    dark: {
      bg: '#002b36', text: '#9fadad', heading: '#eee8d5', muted: '#839496',
      accent: '#268bd2', accent2: '#2aa198',
      red: '#dc322f', green: '#859900', amber: '#b58900', blue: '#268bd2',
      violet: '#6c71c4', pink: '#d33682', orange: '#cb4b16',
    },
    light: {
      bg: '#eee8d5', text: '#4a5a61', heading: '#073642', muted: '#657b83',
      accent: '#268bd2', accent2: '#2aa198',
      red: '#dc322f', green: '#859900', amber: '#b58900', blue: '#268bd2',
      violet: '#6c71c4', pink: '#d33682', orange: '#cb4b16', paper: '#fdf6e3',
    },
  },
  {
    id: 'gruvbox',
    name: 'Gruvbox',
    note: 'Retro warm browns with soft orange and olive.',
    tone: 'warm',
    dark: {
      bg: '#282828', text: '#ebdbb2', heading: '#fbf1c7', muted: '#a89984',
      accent: '#fe8019', accent2: '#83a598',
      red: '#fb4934', green: '#b8bb26', amber: '#fabd2f', blue: '#83a598',
      violet: '#d3869b', pink: '#d3869b', orange: '#fe8019',
    },
    light: {
      bg: '#f2e5bc', text: '#3c3836', heading: '#282828', muted: '#7c6f64',
      accent: '#af3a03', accent2: '#076678',
      red: '#9d0006', green: '#79740e', amber: '#b57614', blue: '#076678',
      violet: '#8f3f71', pink: '#8f3f71', orange: '#af3a03', paper: '#fbf1c7',
    },
  },
  {
    id: 'everforest',
    name: 'Everforest',
    note: 'Muted forest greens, designed to rest the eyes.',
    tone: 'warm',
    dark: {
      bg: '#2d353b', text: '#d3c6aa', heading: '#e8e0c8', muted: '#859289',
      accent: '#a7c080', accent2: '#7fbbb3',
      red: '#e67e80', green: '#a7c080', amber: '#dbbc7f', blue: '#7fbbb3',
      violet: '#d699b6', pink: '#d699b6', orange: '#e69875',
    },
    light: {
      bg: '#efebd4', text: '#5c6a72', heading: '#3a464c', muted: '#829181',
      accent: '#8da101', accent2: '#3a94c5',
      red: '#f85552', green: '#8da101', amber: '#dfa000', blue: '#3a94c5',
      violet: '#df69ba', pink: '#df69ba', orange: '#f57d26', paper: '#fdf6e3',
    },
  },
  {
    id: 'nord',
    name: 'Nord',
    note: 'Arctic slate with frost blue accents.',
    tone: 'cool',
    dark: {
      bg: '#2e3440', text: '#d8dee9', heading: '#eceff4', muted: '#8a93a5',
      accent: '#88c0d0', accent2: '#81a1c1',
      red: '#bf616a', green: '#a3be8c', amber: '#ebcb8b', blue: '#81a1c1',
      violet: '#b48ead', pink: '#b48ead', orange: '#d08770',
    },
    light: {
      bg: '#e5e9f0', text: '#3b4252', heading: '#2e3440', muted: '#5e687a',
      accent: '#5e81ac', accent2: '#4c7a8c',
      red: '#bf616a', green: '#a3be8c', amber: '#ebcb8b', blue: '#5e81ac',
      violet: '#b48ead', pink: '#b48ead', orange: '#d08770', paper: '#f2f4f8',
    },
  },
  {
    id: 'rose-pine',
    name: 'Rosé Pine',
    note: 'Dusky purple with rose and gold. Light mode is Dawn.',
    tone: 'warm',
    dark: {
      bg: '#191724', text: '#e0def4', heading: '#efedfc', muted: '#908caa',
      accent: '#c4a7e7', accent2: '#ebbcba',
      red: '#eb6f92', green: '#9ccfd8', amber: '#f6c177', blue: '#9ccfd8',
      violet: '#c4a7e7', pink: '#ebbcba', orange: '#f6a878',
    },
    light: {
      bg: '#f2e9e1', text: '#575279', heading: '#464261', muted: '#797593',
      accent: '#907aa9', accent2: '#d7827e',
      red: '#b4637a', green: '#56949f', amber: '#ea9d34', blue: '#286983',
      violet: '#907aa9', pink: '#d7827e', orange: '#d7827e', paper: '#fffaf3',
    },
  },
  {
    id: 'catppuccin',
    name: 'Catppuccin',
    note: 'Soothing pastels: Mocha at night, Latte by day.',
    tone: 'cool',
    dark: {
      bg: '#1e1e2e', text: '#cdd6f4', heading: '#e4e9fb', muted: '#9399b2',
      accent: '#cba6f7', accent2: '#89b4fa',
      red: '#f38ba8', green: '#a6e3a1', amber: '#f9e2af', blue: '#89b4fa',
      violet: '#cba6f7', pink: '#f5c2e7', orange: '#fab387',
    },
    light: {
      bg: '#e6e9ef', text: '#4c4f69', heading: '#2f3146', muted: '#6c6f85',
      accent: '#8839ef', accent2: '#1e66f5',
      red: '#d20f39', green: '#40a02b', amber: '#df8e1d', blue: '#1e66f5',
      violet: '#8839ef', pink: '#ea76cb', orange: '#fe640b', paper: '#f4f5f8',
    },
  },
  {
    id: 'kanagawa',
    name: 'Kanagawa',
    note: 'Ink and wave blues after Hokusai. Light mode is Lotus.',
    tone: 'warm',
    dark: {
      bg: '#1f1f28', text: '#dcd7ba', heading: '#ece7cf', muted: '#8a8980',
      accent: '#7e9cd8', accent2: '#e6c384',
      red: '#e46876', green: '#98bb6c', amber: '#e6c384', blue: '#7fb4ca',
      violet: '#957fb8', pink: '#d27e99', orange: '#ffa066',
    },
    light: {
      bg: '#e5ddb0', text: '#545464', heading: '#43436c', muted: '#7a7970',
      accent: '#4d699b', accent2: '#cc6d00',
      red: '#c84053', green: '#6f894e', amber: '#77713f', blue: '#4d699b',
      violet: '#624c83', pink: '#b35b79', orange: '#cc6d00', paper: '#f4efcf',
    },
  },
  {
    id: 'tokyo-night',
    name: 'Tokyo Night',
    note: 'City-night blues and violets. Light mode is Day.',
    tone: 'cool',
    dark: {
      bg: '#1a1b26', text: '#c0caf5', heading: '#d5dcfa', muted: '#8089b3',
      accent: '#7aa2f7', accent2: '#bb9af7',
      red: '#f7768e', green: '#9ece6a', amber: '#e0af68', blue: '#7dcfff',
      violet: '#bb9af7', pink: '#f4a6d7', orange: '#ff9e64',
    },
    light: {
      bg: '#e1e2e7', text: '#343b58', heading: '#262b45', muted: '#6172b0',
      accent: '#2e7de9', accent2: '#9854f1',
      red: '#f52a65', green: '#587539', amber: '#8c6c3e', blue: '#2e7de9',
      violet: '#9854f1', pink: '#c64a8e', orange: '#b15c00', paper: '#eeeff3',
    },
  },
  {
    id: 'flexoki',
    name: 'Flexoki',
    note: 'Ink on paper, tuned for reading in low light.',
    tone: 'warm',
    dark: {
      bg: '#1c1b1a', text: '#cecdc3', heading: '#e6e4d9', muted: '#878580',
      accent: '#3aa99f', accent2: '#da702c',
      red: '#d14d41', green: '#879a39', amber: '#d0a215', blue: '#4385be',
      violet: '#8b7ec8', pink: '#ce5d97', orange: '#da702c',
    },
    light: {
      bg: '#f2f0e5', text: '#343331', heading: '#100f0f', muted: '#6f6e69',
      accent: '#24837b', accent2: '#bc5215',
      red: '#af3029', green: '#66800b', amber: '#ad8301', blue: '#205ea6',
      violet: '#5e409d', pink: '#a02f6f', orange: '#bc5215', paper: '#fffcf0',
    },
  },
  {
    id: 'ayu',
    name: 'Ayu',
    note: 'Mirage blue-gray with a marigold accent.',
    tone: 'cool',
    dark: {
      bg: '#1f2430', text: '#cccac2', heading: '#e6e4dc', muted: '#8a9199',
      accent: '#ffcc66', accent2: '#73d0ff',
      red: '#f28779', green: '#bae67e', amber: '#ffd173', blue: '#73d0ff',
      violet: '#dfbfff', pink: '#f28fad', orange: '#ffa659',
    },
    light: {
      bg: '#eceef0', text: '#4f5459', heading: '#33383d', muted: '#73777c',
      accent: '#c76a1a', accent2: '#399ee6',
      red: '#e65050', green: '#6b9400', amber: '#c78a20', blue: '#2f86c8',
      violet: '#a37acc', pink: '#d0507a', orange: '#d9701c', paper: '#f8f9fa',
    },
  },
  {
    id: 'iceberg',
    name: 'Iceberg',
    note: 'Calm blue-gray with very low saturation.',
    tone: 'cool',
    dark: {
      bg: '#161821', text: '#c6c8d1', heading: '#d8dae3', muted: '#7c8199',
      accent: '#84a0c6', accent2: '#89b8c2',
      red: '#e27878', green: '#b4be82', amber: '#e2a478', blue: '#84a0c6',
      violet: '#a093c7', pink: '#c792b4', orange: '#e2a478',
    },
    light: {
      bg: '#e8e9ec', text: '#33374c', heading: '#22253a', muted: '#6b7089',
      accent: '#2d539e', accent2: '#3f83a6',
      red: '#cc517a', green: '#668e3d', amber: '#c57339', blue: '#2d539e',
      violet: '#7759b4', pink: '#9c4a7f', orange: '#c57339', paper: '#f3f4f6',
    },
  },
  {
    id: 'github',
    name: 'GitHub',
    note: 'Familiar and neutral. Dark mode is Dimmed.',
    tone: 'neutral',
    dark: {
      bg: '#22272e', text: '#adbac7', heading: '#cdd9e5', muted: '#7d8a97',
      accent: '#539bf5', accent2: '#dcbdfb',
      red: '#f47067', green: '#57ab5a', amber: '#c69026', blue: '#539bf5',
      violet: '#dcbdfb', pink: '#fc8dc7', orange: '#f69d50',
    },
    light: {
      bg: '#eaeef2', text: '#24292f', heading: '#1f2328', muted: '#59636e',
      accent: '#0969da', accent2: '#8250df',
      red: '#cf222e', green: '#1a7f37', amber: '#9a6700', blue: '#0969da',
      violet: '#8250df', pink: '#bf3989', orange: '#bc4c00', paper: '#f6f8fa',
    },
  },
  {
    id: 'melange',
    name: 'Melange',
    note: 'Cozy coffee browns with gold and sage.',
    tone: 'warm',
    dark: {
      bg: '#292522', text: '#ece1d7', heading: '#f4ede6', muted: '#a38d78',
      accent: '#ebc06d', accent2: '#a3a9ce',
      red: '#d47766', green: '#85b695', amber: '#ebc06d', blue: '#a3a9ce',
      violet: '#cf9bc2', pink: '#cf9bc2', orange: '#e49b5d',
    },
    light: {
      bg: '#ebe5df', text: '#54433a', heading: '#3f312a', muted: '#7d6658',
      accent: '#bc5c00', accent2: '#465aa4',
      red: '#bf0021', green: '#3a684a', amber: '#a06d00', blue: '#465aa4',
      violet: '#904180', pink: '#904180', orange: '#bc5c00', paper: '#f7f3ef',
    },
  },
  {
    id: 'one',
    name: 'One',
    note: 'The balanced Atom One colors on charcoal and soft gray.',
    tone: 'neutral',
    dark: {
      bg: '#282c34', text: '#abb2bf', heading: '#d7dae0', muted: '#7f848e',
      accent: '#61afef', accent2: '#c678dd',
      red: '#e06c75', green: '#98c379', amber: '#e5c07b', blue: '#61afef',
      violet: '#c678dd', pink: '#e88aa6', orange: '#d19a66',
    },
    light: {
      bg: '#ececed', text: '#383a42', heading: '#232428', muted: '#696c77',
      accent: '#4078f2', accent2: '#a626a4',
      red: '#e45649', green: '#50a14f', amber: '#c18401', blue: '#4078f2',
      violet: '#a626a4', pink: '#ca1243', orange: '#986801', paper: '#f7f7f8',
    },
  },
  {
    id: 'dracula',
    name: 'Dracula',
    note: 'Purple and pink on dusk gray. Light mode is Alucard.',
    tone: 'cool',
    dark: {
      bg: '#282a36', text: '#e6e6e0', heading: '#f4f4ee', muted: '#8b95c0',
      accent: '#bd93f9', accent2: '#ff79c6',
      red: '#ff6e6e', green: '#69e08f', amber: '#f1fa8c', blue: '#8be9fd',
      violet: '#bd93f9', pink: '#ff79c6', orange: '#ffb86c',
    },
    light: {
      bg: '#efeadb', text: '#2c2b25', heading: '#1f1f1f', muted: '#6c664b',
      accent: '#644ac9', accent2: '#a3144d',
      red: '#cb3a2a', green: '#14710a', amber: '#846e15', blue: '#036a96',
      violet: '#644ac9', pink: '#a3144d', orange: '#a34d14', paper: '#fbf7e6',
    },
  },
  {
    id: 'paper',
    name: 'Sepia',
    note: 'Warm paper and candlelight browns, the least blue light.',
    tone: 'warm',
    dark: {
      bg: '#1e1a16', text: '#d6c8b4', heading: '#eadfcc', muted: '#9c8e7c',
      accent: '#d9a066', accent2: '#9fb38a',
      red: '#e08a78', green: '#a8bf8a', amber: '#e0b26a', blue: '#93acc0',
      violet: '#b9a0c8', pink: '#d69bab', orange: '#dc935a',
    },
    light: {
      bg: '#ece2d0', text: '#433829', heading: '#2e251a', muted: '#76695a',
      accent: '#9a5b1e', accent2: '#4f6f52',
      red: '#a63d2a', green: '#4f6f2a', amber: '#875f10', blue: '#3d6380',
      violet: '#6d4f80', pink: '#93405a', orange: '#a0521a', paper: '#f6efe2',
    },
  },
];

export function findPalette(id: string | null | undefined): Palette {
  return PALETTES.find((p) => p.id === id) ?? PALETTES[0];
}
