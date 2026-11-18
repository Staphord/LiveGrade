(function () {
  document.querySelectorAll('[data-poll-response-form]').forEach(function (form) {
    const submit = form.querySelector('[data-response-submit]');
    const summary = form.querySelector('[data-response-summary]');

    function clearQuestion(question) {
      question.classList.remove('is-invalid');
      question.querySelectorAll('[aria-invalid="true"]').forEach(function (field) {
        field.removeAttribute('aria-invalid');
      });
      const error = question.querySelector('[data-response-error]');
      if (error) error.textContent = '';
    }

    function showQuestionError(question, message) {
      question.classList.add('is-invalid');
      const error = question.querySelector('[data-response-error]');
      if (error) error.textContent = message;
      question.querySelectorAll('input, textarea').forEach(function (field) {
        field.setAttribute('aria-invalid', 'true');
      });
    }

    form.addEventListener('change', function (event) {
      const question = event.target.closest('[data-response-question]');
      if (!question) return;
      if (event.target.matches('[data-response-abstain]') && event.target.checked) {
        question.querySelectorAll('[data-response-choice]').forEach(function (choice) {
          choice.checked = false;
        });
      } else if (event.target.matches('[data-response-choice]') && event.target.checked) {
        const abstain = question.querySelector('[data-response-abstain]');
        if (abstain) abstain.checked = false;
      }
      clearQuestion(question);
      if (summary) summary.hidden = true;
    });

    function autoSize(field) {
      field.style.height = 'auto';
      field.style.height = field.scrollHeight + 'px';
    }

    form.querySelectorAll('[data-autosize]').forEach(autoSize);

    form.addEventListener('keydown', function (event) {
      if (event.key === 'Enter' && event.target.classList.contains('poll-short-answer')) {
        event.preventDefault();
      }
    });

    form.addEventListener('input', function (event) {
      if (!event.target.matches('[data-response-text]')) return;
      if (event.target.hasAttribute('data-autosize')) autoSize(event.target);
      const question = event.target.closest('[data-response-question]');
      clearQuestion(question);
      const counter = question.querySelector('[data-response-char-count]');
      if (counter) {
        const limit = event.target.maxLength;
        counter.textContent = event.target.value.length + '/' + limit.toLocaleString();
      }
      if (summary) summary.hidden = true;
    });

    form.addEventListener('submit', function (event) {
      let firstInvalid = null;
      form.querySelectorAll('[data-response-question]').forEach(function (question) {
        clearQuestion(question);
        const text = question.querySelector('[data-response-text]');
        const choices = Array.from(question.querySelectorAll('[data-response-choice]'));
        const abstain = question.querySelector('[data-response-abstain]');
        let message = '';
        if (text) {
          if (text.required && !text.value.trim()) message = 'Answer this required question.';
          else if (text.value.length > text.maxLength) message = 'Keep your answer within the character limit.';
        } else {
          const selected = choices.filter(function (choice) { return choice.checked; });
          if (abstain && abstain.checked && selected.length) {
            message = 'Choose an answer or “None of these options”, not both.';
          } else if (question.getAttribute('aria-required') === 'true' && !selected.length && !(abstain && abstain.checked)) {
            message = abstain
              ? 'Answer this required question.'
              : 'Answer this required question.';
          } else if (choices.some(function (choice) { return choice.type === 'radio' && choice.checked; }) && selected.length > 1) {
            message = 'Choose only one answer for this question.';
          }
        }
        if (message) {
          showQuestionError(question, message);
          if (!firstInvalid) firstInvalid = text || choices[0] || abstain;
        }
      });

      if (firstInvalid) {
        event.preventDefault();
        if (summary && summary.textContent.trim()) {
          summary.hidden = false;
        }
        firstInvalid.focus();
        return;
      }
      if (submit) {
        submit.disabled = true;
        submit.textContent = 'Submitting…';
      }
    });
  });
})();
