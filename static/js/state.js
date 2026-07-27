export const state = {
    columns: [],
    cards: [],
    revision: 1,
    filters: {
        due: "all",
        priority: "all",
        labels: [],
    },
    sortByColumn: {},
    cardDescriptionDisplay: "two-lines",
    quickCreate: { columnId: null, title: "", submitting: false },
    todayQuickCreate: { title: "", submitting: false },
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
    historySort: "archived_desc",
};

export function hasActiveFilters() {
    return state.filters.due !== "all" || state.filters.priority !== "all" || state.filters.labels.length > 0;
}

export function isManualReorderDisabled() { return hasActiveFilters(); }
