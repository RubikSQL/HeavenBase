/** Shared UI state contracts. */

import type { DashboardPreferencesRequest, JsonRecord, WorkspaceHealthResponse } from "./api.js";
import type { Language } from "./i18n.js";
import type { UiElement } from "./ui.js";

type ThemePreference = NonNullable<DashboardPreferencesRequest["theme"]>;
type FontPreference = NonNullable<DashboardPreferencesRequest["font"]>;
type FontScalePreference = NonNullable<DashboardPreferencesRequest["font_scale"]>;

export interface ThemeSpec {
  cls: ThemePreference;
  name: string;
  icon: string;
}

export interface EntityOpenOptions {
  objectId?: string;
  mode?: "data" | "schema";
}

export interface AppearanceState {
  themes(): readonly ThemeSpec[];
  theme(): ThemePreference;
  setTheme(theme: ThemePreference): Promise<void>;
  lang(): Language;
  setLang(language: Language): Promise<void>;
  font(): FontPreference;
  setFont(name: FontPreference): Promise<void>;
  fontScale(): FontScalePreference;
  setFontScale(name: FontScalePreference): Promise<void>;
}

export interface DashboardState {
  route: string;
  wsId: string | null;
  health: WorkspaceHealthResponse | null;
  appearance: AppearanceState;
  refreshStatus(): void;
  openInspector(content: Node | string, title?: string): void;
  closeInspector(): void;
  openEntity(entityId: string, options?: EntityOpenOptions): void;
  navigate(route: string): void;
  assistContext(): JsonRecord;
  setAssistantScope(scope: string | null): void;
}

export interface DashboardView {
  renderBrowser(container: UiElement, state: DashboardState): void;
  renderMain(container: UiElement, state: DashboardState): void;
}
