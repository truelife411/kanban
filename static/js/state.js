export const state = {
    columns: [],
    cards: [],
    revision: 1,
    filters: { due: "all", priority: "all", labels: [] },
    quickCreate: { columnId: null, title: "", submitting: false },
    currentCardId: null,
    currentCardCol: null,
    currentCardDraft: false,
    currentCardVersion: null,
    attachments: [],
    attachmentQueue: [],
    attachmentAbort: null,
    uploading: false,
    initialCardSnapshot: null,
    initialColumnName: "",
    saving: false,
    busyOperation: "",
    modalOpener: null,
    searchAbort: null,
    searchCursor: null,
    searchParams: "",
};

export function hasActiveFilters() {
    return state.filters.due !== "all" || state.filters.priority !== "all" || state.filters.labels.length > 0;
}
