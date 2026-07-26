document.body.addEventListener('htmx:beforeRequest', function(event) {
    var message = document.getElementById('message-input').value;
    var chatContainer = document.getElementById('chat-container');
    
    // Append user message immediately
    var userMessage = document.createElement('div');
    userMessage.className = 'message user-message';
    userMessage.innerHTML = `<p>${message}</p>`;
    chatContainer.appendChild(userMessage);
    
    // Trigger reflow to ensure the transition happens
    userMessage.offsetHeight;
    
    // Add 'show' class to trigger animation
    userMessage.classList.add('show');
    
    // Clear input field
    document.getElementById('message-input').value = '';
    
    // Append typing indicator
    var typingIndicator = document.getElementById('typing-indicator').content.cloneNode(true);
    chatContainer.appendChild(typingIndicator);
    
    // Scroll to bottom
    chatContainer.scrollTop = chatContainer.scrollHeight;
});

document.body.addEventListener('htmx:afterSwap', function(event) {
    var chatContainer = document.getElementById('chat-container');
    
    // Remove typing indicator
    var typingIndicator = chatContainer.querySelector('.typing-indicator');
    if (typingIndicator) {
        typingIndicator.remove();
    }
    
    // Get the newly added bot message
    var newMessage = chatContainer.lastElementChild;
    
    // Trigger reflow to ensure the transition happens
    newMessage.offsetHeight;
    
    // Add 'show' class to trigger animation
    newMessage.classList.add('show');
    
    // Scroll to bottom
    chatContainer.scrollTop = chatContainer.scrollHeight;
});

// Scroll to bottom on page load
window.onload = function() {
    var chatContainer = document.getElementById('chat-container');
    chatContainer.scrollTop = chatContainer.scrollHeight;
};

// "Speak responses aloud" checkbox — persisted client-side only (localStorage),
// consistent with this app's no-server-persistence design for UI preferences.
// Restored on load, saved on every change.
var AUDIO_MODE_STORAGE_KEY = 'gfrag_audio_mode';

document.addEventListener('DOMContentLoaded', function() {
    var audioModeCheckbox = document.getElementById('audio-mode-checkbox');
    if (!audioModeCheckbox) {
        return;
    }

    audioModeCheckbox.checked = localStorage.getItem(AUDIO_MODE_STORAGE_KEY) === 'true';

    audioModeCheckbox.addEventListener('change', function() {
        localStorage.setItem(AUDIO_MODE_STORAGE_KEY, audioModeCheckbox.checked ? 'true' : 'false');
    });
});
