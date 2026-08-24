const form = document.getElementById('track-form');
const input = document.getElementById('tracking-input');
const notice = document.getElementById('track-notice');
const resultPanel = document.getElementById('track-result');
const STATUS_ORDER = ['submitted', 'in_review', 'resolved'];
const STATUS_LABELS = { submitted: 'Submitted', in_review: 'In review', resolved: 'Resolved' };

function showNotice(message, type) {
  notice.textContent = message;
  notice.className = `notice show ${type}`;
}

async function trackComplaint(code) {
  const normalized = code.trim().toUpperCase();
  if (!/^CP-[A-Z0-9]{10}$/.test(normalized)) {
    resultPanel.hidden = true;
    showNotice('Enter a tracking code in the format CP-XXXXXXXXXX.', 'error');
    return;
  }
  notice.className = 'notice';
  try {
    const response = await fetch(`/complaints/track/${encodeURIComponent(normalized)}`);
    if (response.status === 404) throw new Error('not-found');
    if (!response.ok) throw new Error('server');
    const result = await response.json();
    const activeIndex = STATUS_ORDER.indexOf(result.status);
    document.querySelectorAll('.step').forEach((step, index) => {
      step.classList.toggle('done', index <= activeIndex);
    });
    document.getElementById('result-code').textContent = result.tracking_code;
    document.getElementById('result-status').textContent = STATUS_LABELS[result.status] || result.status;
    document.getElementById('result-category').textContent = result.category;
    document.getElementById('result-region').textContent = result.region;
    document.getElementById('result-date').textContent = new Date(result.created_at).toLocaleString();
    resultPanel.hidden = false;
    history.replaceState(null, '', `?id=${encodeURIComponent(normalized)}`);
  } catch (error) {
    resultPanel.hidden = true;
    showNotice(
      error.message === 'not-found'
        ? 'No complaint was found with that code. Check the letters and numbers and try again.'
        : 'The tracking service is temporarily unavailable. Please try again.',
      'error'
    );
  }
}

form.addEventListener('submit', event => {
  event.preventDefault();
  trackComplaint(input.value);
});

const initialCode = new URLSearchParams(location.search).get('id');
if (initialCode) {
  input.value = initialCode;
  trackComplaint(initialCode);
}
