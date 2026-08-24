const textModeButton = document.getElementById('text-mode');
const voiceModeButton = document.getElementById('voice-mode');
const textField = document.getElementById('text-field');
const voiceBox = document.getElementById('voice-box');
const recordButton = document.getElementById('record-button');
const voiceStatus = document.getElementById('voice-status');
const audioPreview = document.getElementById('audio-preview');
const notice = document.getElementById('form-notice');
const locationResult = document.getElementById('location-result');
const submitButton = document.getElementById('submit-button');
const photoInput = document.getElementById('complaint-photo');
const photoPreview = document.getElementById('photo-preview');

let mode = 'text';
let resolvedLocation = null;
let mediaRecorder = null;
let audioChunks = [];
let audioBlob = null;

photoInput.addEventListener('change', () => {
  const photo = photoInput.files[0];
  if (!photo) {
    photoPreview.hidden = true;
    photoPreview.removeAttribute('src');
    return;
  }
  if (photo.size > 5 * 1024 * 1024) {
    photoInput.value = '';
    photoPreview.hidden = true;
    setNotice('Please choose a photo smaller than 5 MB.', 'error');
    return;
  }
  photoPreview.src = URL.createObjectURL(photo);
  photoPreview.hidden = false;
});

function deviceCitizenId() {
  let id = localStorage.getItem('citizen_voice_device_id');
  if (!id) {
    id = crypto.randomUUID ? crypto.randomUUID() : `web-${Date.now()}-${Math.random()}`;
    localStorage.setItem('citizen_voice_device_id', id);
  }
  return `web:${id}`;
}

function setNotice(message, type) {
  notice.textContent = message;
  notice.className = `notice show ${type}`;
}

function setMode(nextMode) {
  mode = nextMode;
  const isText = mode === 'text';
  textModeButton.classList.toggle('active', isText);
  voiceModeButton.classList.toggle('active', !isText);
  textField.style.display = isText ? '' : 'none';
  voiceBox.classList.toggle('show', !isText);
}

textModeButton.addEventListener('click', () => setMode('text'));
voiceModeButton.addEventListener('click', () => setMode('voice'));

function showResolvedLocation(location) {
  resolvedLocation = location;
  locationResult.textContent = `Location confirmed: ${location.display_name}`;
  locationResult.classList.add('show');
}

document.getElementById('find-area').addEventListener('click', async () => {
  const query = document.getElementById('area-input').value.trim();
  if (query.length < 2) {
    setNotice('Please type an area, village or city name.', 'error');
    return;
  }
  locationResult.textContent = 'Finding your area…';
  locationResult.classList.add('show');
  resolvedLocation = null;
  try {
    const response = await fetch(`/locations/geocode?query=${encodeURIComponent(query)}`);
    if (!response.ok) throw new Error('not-found');
    showResolvedLocation(await response.json());
    notice.className = 'notice';
  } catch (_) {
    locationResult.classList.remove('show');
    setNotice('We could not find that place. Try adding your district or state.', 'error');
  }
});

document.getElementById('use-location').addEventListener('click', () => {
  if (!navigator.geolocation) {
    setNotice('Location access is unavailable. Please type your area instead.', 'error');
    return;
  }
  locationResult.textContent = 'Reading your location…';
  locationResult.classList.add('show');
  navigator.geolocation.getCurrentPosition(async position => {
    try {
      const params = new URLSearchParams({
        latitude: position.coords.latitude,
        longitude: position.coords.longitude,
      });
      const response = await fetch(`/locations/reverse?${params}`);
      if (!response.ok) throw new Error('reverse-failed');
      showResolvedLocation(await response.json());
      notice.className = 'notice';
    } catch (_) {
      setNotice('We could not identify that location. Please type your area.', 'error');
    }
  }, () => {
    locationResult.classList.remove('show');
    setNotice('Location permission was not available. Please type your area.', 'error');
  }, { enableHighAccuracy: false, timeout: 10000 });
});

recordButton.addEventListener('click', async () => {
  if (mediaRecorder && mediaRecorder.state === 'recording') {
    mediaRecorder.stop();
    return;
  }
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    audioChunks = [];
    mediaRecorder = new MediaRecorder(stream);
    mediaRecorder.addEventListener('dataavailable', event => audioChunks.push(event.data));
    mediaRecorder.addEventListener('stop', () => {
      audioBlob = new Blob(audioChunks, { type: mediaRecorder.mimeType || 'audio/webm' });
      audioPreview.src = URL.createObjectURL(audioBlob);
      audioPreview.hidden = false;
      voiceStatus.textContent = 'Recording ready. You can listen before submitting.';
      recordButton.textContent = '●';
      recordButton.classList.remove('recording');
      stream.getTracks().forEach(track => track.stop());
    });
    mediaRecorder.start();
    recordButton.textContent = '■';
    recordButton.classList.add('recording');
    voiceStatus.textContent = 'Recording… tap again to stop';
  } catch (_) {
    setNotice('Microphone access was not available. You can submit your complaint as text.', 'error');
  }
});

document.getElementById('complaint-form').addEventListener('submit', async event => {
  event.preventDefault();
  notice.className = 'notice';

  if (!resolvedLocation) {
    setNotice('Please find or share your location before submitting.', 'error');
    return;
  }
  if (!document.getElementById('consent').checked) {
    setNotice('Please confirm the location and data-use statement.', 'error');
    return;
  }
  const text = document.getElementById('complaint-text').value.trim();
  if (mode === 'text' && text.length < 5) {
    setNotice('Please describe the problem in a little more detail.', 'error');
    return;
  }
  if (mode === 'voice' && !audioBlob) {
    setNotice('Please record a voice complaint before submitting.', 'error');
    return;
  }

  const data = new FormData();
  data.append('citizen_id', deviceCitizenId());
  data.append('latitude', resolvedLocation.latitude);
  data.append('longitude', resolvedLocation.longitude);
  data.append('region', resolvedLocation.region);
  if (photoInput.files[0]) data.append('photo', photoInput.files[0]);
  let endpoint = '/complaints/text';
  if (mode === 'text') {
    data.append('text', text);
  } else {
    endpoint = '/complaints/voice';
    data.append('audio', audioBlob, 'citizen-voice.webm');
    data.append('language_code', document.getElementById('voice-language').value);
  }

  submitButton.disabled = true;
  submitButton.textContent = 'Submitting…';
  try {
    const response = await fetch(endpoint, { method: 'POST', body: data });
    const result = await response.json();
    if (!response.ok) throw new Error(result.detail || 'Submission failed');

    notice.textContent = '';
    notice.className = 'notice show success';
    const title = document.createElement('strong');
    title.textContent = 'Complaint submitted successfully.';
    const understanding = document.createElement('p');
    understanding.textContent = result.ai_provider === 'gemini'
      ? `Gemini understood this as ${result.category}: ${result.ai_summary}`
      : `Your report was categorized as ${result.category}.`;
    const code = document.createElement('span');
    code.className = 'tracking-code';
    code.textContent = result.tracking_code;
    const copy = document.createElement('button');
    copy.type = 'button';
    copy.className = 'button secondary';
    copy.textContent = 'Copy code';
    copy.addEventListener('click', () => navigator.clipboard.writeText(result.tracking_code));
    const track = document.createElement('a');
    track.className = 'button';
    track.href = `/citizen/track.html?id=${encodeURIComponent(result.tracking_code)}`;
    track.textContent = 'Track complaint';
    const actions = document.createElement('div');
    actions.className = 'result-actions';
    actions.append(copy, track);
    notice.append(title, understanding, code, actions);
    document.getElementById('complaint-form').reset();
    resolvedLocation = null;
    locationResult.classList.remove('show');
    audioBlob = null;
    audioPreview.hidden = true;
    photoPreview.hidden = true;
    photoPreview.removeAttribute('src');
  } catch (error) {
    setNotice(error.message || 'We could not submit your complaint. Please try again.', 'error');
  } finally {
    submitButton.disabled = false;
    submitButton.textContent = 'Submit complaint';
  }
});
