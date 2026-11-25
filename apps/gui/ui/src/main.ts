/** Initialize the TypeScript-owned edge of the dashboard UI. */
export function initializeUiRuntime(): void {
  document.documentElement.dataset.uiContract = "heavenbase-gui-v1";
}
