const rows = {
  'studio-jobs': [
    { label: 'Sample', task: 'Review Strategy', status: 'Ready', count: 3 },
    { label: 'Sample', task: 'Check Risk level', status: 'Waiting', count: 2 },
    { label: 'Sample', task: 'Compare Bet size', status: 'Complete', count: 4 },
  ],
  'studio-capabilities': [
    { label: 'Sample', feature: 'Strategy', available: 4 },
    { label: 'Sample', feature: 'Market', available: 3 },
    { label: 'Sample', feature: 'Cash out', available: 2 },
  ],
};
Object.values(rows).forEach(values => { values.forEach(Object.freeze); Object.freeze(values); });
export const kashbotSamplePack = Object.freeze({ id: 'kashbot-sample', label: 'Sample', rows: Object.freeze(rows) });
