(function () {
  const wrapperSelector = '[data-datetime-picker]';
  const inputSelector = 'input[type="date"], input[type="time"], input[type="datetime-local"]';
  const deadlineEntrySelector = '[data-deadline-entry]';

  function initializeDateTimePickers(root) {
    const scope = root || document;
    const wrappers = [];
    if (scope.matches && scope.matches(wrapperSelector)) wrappers.push(scope);
    if (scope.querySelectorAll) wrappers.push(...scope.querySelectorAll(wrapperSelector));

    wrappers.forEach(function (wrapper) {
      if (wrapper.dataset.datetimePickerReady) return;
      const input = wrapper.querySelector(inputSelector);
      const openButton = wrapper.querySelector('[data-datetime-open]');
      const clearButton = wrapper.querySelector('[data-datetime-clear]');
      if (!input || !openButton) return;

      wrapper.dataset.datetimePickerReady = 'true';
      wrapper.classList.add('has-enhanced-picker');
      openButton.hidden = false;
      if (typeof input.showPicker === 'function') wrapper.classList.add('has-picker-api');

      function syncClearButton() {
        if (clearButton) clearButton.hidden = !input.value;
      }

      openButton.addEventListener('click', function () {
        if (input.disabled || input.readOnly) return;
        if (typeof input.showPicker === 'function') {
          try {
            input.showPicker();
            return;
          } catch (error) {
            // Older engines or embedded contexts can reject showPicker().
          }
        }
        input.focus();
      });

      if (clearButton) {
        clearButton.addEventListener('click', function () {
          input.value = '';
          input.dispatchEvent(new Event('input', {bubbles: true}));
          input.dispatchEvent(new Event('change', {bubbles: true}));
          input.focus();
        });
        input.addEventListener('input', syncClearButton);
        input.addEventListener('change', syncClearButton);
      }

      if (input.form) {
        input.form.addEventListener('reset', function () {
          window.setTimeout(syncClearButton, 0);
        });
      }
      syncClearButton();
    });
  }

  function initializeDeadlineEntries(root) {
    const scope = root || document;
    const entries = [];
    if (scope.matches && scope.matches(deadlineEntrySelector)) entries.push(scope);
    if (scope.querySelectorAll) entries.push(...scope.querySelectorAll(deadlineEntrySelector));

    entries.forEach(function (entry) {
      if (entry.dataset.deadlineEntryReady) return;
      const textInput = entry.querySelector('.form-control');
      const pickerInput = entry.querySelector('[data-deadline-native]');
      const pickerButton = entry.querySelector('[data-deadline-open]');
      if (!textInput || !pickerInput || !pickerButton) return;
      entry.dataset.deadlineEntryReady = 'true';

      function syncNativeValue() {
        if (entry.dataset.deadlineEntry === 'date') {
          const match = textInput.value.trim().match(/^(\d{2})\/(\d{2})\/(\d{4})$/);
          if (!match) { pickerInput.value = ''; return; }
          const day = Number(match[1]);
          const month = Number(match[2]);
          const year = Number(match[3]);
          const daysInMonth = new Date(year, month, 0).getDate();
          pickerInput.value = month >= 1 && month <= 12 && day >= 1 && day <= daysInMonth
            ? year + '-' + match[2] + '-' + match[1] : '';
        } else {
          pickerInput.value = /^([01]\d|2[0-3]):([0-5]\d)$/.test(textInput.value.trim())
            ? textInput.value.trim() : '';
        }
      }

      pickerInput.addEventListener('input', function () {
        if (entry.dataset.deadlineEntry === 'date' && pickerInput.value) {
          const parts = pickerInput.value.split('-');
          textInput.value = parts[2] + '/' + parts[1] + '/' + parts[0];
        } else if (entry.dataset.deadlineEntry === 'time') {
          textInput.value = pickerInput.value;
        }
      });
      pickerInput.addEventListener('change', function () {
        pickerInput.dispatchEvent(new Event('input', {bubbles: true}));
        textInput.dispatchEvent(new Event('input', {bubbles: true}));
        textInput.dispatchEvent(new Event('change', {bubbles: true}));
      });
      textInput.addEventListener('input', syncNativeValue);
      syncNativeValue();

      if (typeof pickerInput.showPicker === 'function') {
        pickerButton.hidden = false;
        pickerButton.addEventListener('click', function () {
          syncNativeValue();
          try {
            pickerInput.showPicker();
          } catch (error) {
            textInput.focus();
          }
        });
      }
    });
  }

  window.initializeDateTimePickers = initializeDateTimePickers;
  window.initializeDeadlineEntries = initializeDeadlineEntries;
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', function () {
      initializeDateTimePickers(document);
      initializeDeadlineEntries(document);
    }, {once: true});
  } else {
    initializeDateTimePickers(document);
    initializeDeadlineEntries(document);
  }
})();
