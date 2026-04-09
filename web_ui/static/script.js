document.addEventListener('DOMContentLoaded', () => {
    const languageFilter = document.getElementById('language-filter');
    const modelSelect = document.getElementById('model-select');
    const saveLocationBtn = document.getElementById('save-location');
    const textInput = document.getElementById('text-input');
    const generateBtn = document.getElementById('generate-btn');
    const translateBtn = document.getElementById('translate-btn');
    const translateLang = document.getElementById('translate-lang');
    const outputsContainer = document.getElementById('outputs-container');
    const themeToggle = document.getElementById('theme-toggle');
    const themeIcon = document.querySelector('.theme-icon');

    // Theme initialization
    const savedTheme = localStorage.getItem('pollux-theme') || 'dark';
    if (savedTheme === 'light') {
        document.body.classList.add('light-theme');
        themeIcon.textContent = '☀️';
    }

    themeToggle.addEventListener('click', () => {
        document.body.classList.toggle('light-theme');
        const isLight = document.body.classList.contains('light-theme');
        localStorage.setItem('pollux-theme', isLight ? 'light' : 'dark');
        themeIcon.textContent = isLight ? '☀️' : '🌙';
    });
    
    let voicesData = {}; // Store the raw voices JSON
    let generatedHistory = []; // Local history of generated clips

    // Load available voices
    async function loadVoices() {
        try {
            const res = await fetch('/api/voices');
            if(!res.ok) throw new Error("Failed to fetch voices");
            voicesData = await res.json();
            
            // Extract distinct languages
            const languages = new Set();
            for(const [modelId, details] of Object.entries(voicesData)) {
                if(details.language && details.language.name_english) {
                    languages.add(details.language.name_english);
                }
            }

            // Populate language filter
            Array.from(languages).sort().forEach(lang => {
                const option = document.createElement('option');
                option.value = lang;
                option.textContent = lang;
                languageFilter.appendChild(option);
            });

            // Populate all voices initially
            populateVoices('all');

        } catch (error) {
            console.error(error);
            alert("Error loading voices check console: " + error.message);
        }
    }

    // Populate model dropdown
    function populateVoices(filterLang) {
        // Clear except first disabled option
        modelSelect.innerHTML = '<option value="" disabled selected>Select a Voice...</option>';
        
        const sortedModels = Object.entries(voicesData).sort((a,b) => a[0].localeCompare(b[0]));
        
        for(const [modelId, details] of sortedModels) {
            if(filterLang === 'all' || (details.language && details.language.name_english === filterLang)) {
                const option = document.createElement('option');
                option.value = modelId;
                const spkCount = details.num_speakers > 1 ? ` (${details.num_speakers} speakers)` : '';
                option.textContent = `${modelId} - ${details.quality}${spkCount}`;
                modelSelect.appendChild(option);
            }
        }
        
        // Disable generate button if model not selected yet? (It defaults to empty, so button validation handles it)
    }

    // Translate logic
    translateBtn.addEventListener('click', async () => {
        const text = textInput.value.trim();
        const target_lang = translateLang.value;
        if (!text) return alert("Please enter some text to translate.");
        
        translateBtn.textContent = 'Translating...';
        translateBtn.disabled = true;

        try {
            const res = await fetch('/api/translate', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ text, target_lang })
            });
            const data = await res.json();
            if(!res.ok) throw new Error(data.detail || "Translation failed");
            
            textInput.value = data.translated_text;
        } catch(error) {
            console.error(error);
            alert("Translation error: " + error.message);
        } finally {
            translateBtn.textContent = 'Translate';
            translateBtn.disabled = false;
        }
    });

    languageFilter.addEventListener('change', (e) => {
        populateVoices(e.target.value);
    });

    // Generate Audio
    generateBtn.addEventListener('click', async () => {
        const text = textInput.value.trim();
        const model = modelSelect.value;
        const saveLocation = saveLocationBtn.value.trim();

        if(!text) return alert("Please enter text.");
        if(!model) return alert("Please select a voice model.");

        setLoading(true);

        try {
            const res = await fetch('/api/generate', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    text: text,
                    model: model,
                    save_location: saveLocation
                })
            });
            const data = await res.json();
            
            if(!res.ok) {
                throw new Error(data.detail || "Generation failed.");
            }

            // Success
            generatedHistory.unshift({
                filename: data.filename,
                url: data.audio_url,
                saveLocation: data.saved_location,
                timestamp: new Date().toLocaleTimeString(),
                model: model
            });
            renderHistory();
            
        } catch (error) {
            console.error(error);
            alert("Generation Error: " + error.message);
        } finally {
            setLoading(false);
        }
    });

    function setLoading(isLoading) {
        const btnText = document.querySelector('.btn-text');
        const loader = document.querySelector('.loader');
        
        if(isLoading) {
            generateBtn.disabled = true;
            btnText.classList.add('hidden');
            loader.classList.remove('hidden');
        } else {
            generateBtn.disabled = false;
            btnText.classList.remove('hidden');
            loader.classList.add('hidden');
        }
    }

    function renderHistory() {
        if(generatedHistory.length === 0) {
            outputsContainer.innerHTML = `
                <div class="empty-state">
                    <p>No audio generated yet. Write a script and let Piper speak!</p>
                </div>
            `;
            return;
        }

        outputsContainer.innerHTML = '';
        generatedHistory.forEach((item, index) => {
            const el = document.createElement('div');
            el.className = 'output-item';
            el.innerHTML = `
                <div class="output-info">
                    <strong>${item.model}</strong>
                    <span class="time">${item.timestamp}</span>
                </div>
                <div class="output-path" title="${item.saveLocation}">
                    ${item.saveLocation}
                </div>
                <div class="output-controls">
                    <audio controls src="${item.url}"></audio>
                    <button class="icon-btn delete-btn" data-index="${index}" data-filename="${item.filename}" aria-label="Delete">
                        ✕
                    </button>
                </div>
            `;
            outputsContainer.appendChild(el);
        });

        // Add delete listeners
        document.querySelectorAll('.delete-btn').forEach(btn => {
            btn.addEventListener('click', async (e) => {
                const idx = parseInt(e.target.getAttribute('data-index'));
                const file = e.target.getAttribute('data-filename');
                
                if(confirm("Delete this audio clip?")) {
                    try {
                        await fetch(`/api/audio/${file}`, { method: 'DELETE' });
                        generatedHistory.splice(idx, 1);
                        renderHistory();
                    } catch(err) {
                        alert("Delete failed: " + err.message);
                    }
                }
            });
        });
    }

    // --- MUSIC TAB LOGIC ---
    const tabVoice = document.getElementById('tab-voice');
    const tabMusic = document.getElementById('tab-music');
    const voiceView = document.getElementById('voice-view');
    const musicView = document.getElementById('music-view');

    tabVoice.addEventListener('click', () => {
        tabVoice.classList.add('active');
        tabMusic.classList.remove('active');
        voiceView.classList.remove('hidden');
        musicView.classList.add('hidden');
    });

    tabMusic.addEventListener('click', () => {
        tabMusic.classList.add('active');
        tabVoice.classList.remove('active');
        musicView.classList.remove('hidden');
        voiceView.classList.add('hidden');
    });

    const generateMusicBtn = document.getElementById('generate-music-btn');
    const musicPromptInput = document.getElementById('music-prompt');
    const musicDurationInput = document.getElementById('music-duration');
    const musicOutputsContainer = document.getElementById('music-outputs-container');
    const musicLoader = document.querySelector('.music-loader');
    const musicBtnText = document.querySelector('.music-btn-text');

    let generatedMusicHistory = [];

    generateMusicBtn.addEventListener('click', async () => {
        const prompt = musicPromptInput.value.trim();
        const duration = parseInt(musicDurationInput.value, 10);

        if(!prompt) return alert("Please enter a music description.");
        
        generateMusicBtn.disabled = true;
        musicBtnText.classList.add('hidden');
        musicLoader.classList.remove('hidden');

        try {
            const res = await fetch('/api/generate_music', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ prompt, duration })
            });
            const data = await res.json();
            
            if(!res.ok) throw new Error(data.detail || "Music generation failed.");

            // Success
            generatedMusicHistory.unshift({
                filename: data.filename,
                url: data.audio_url,
                timestamp: new Date().toLocaleTimeString(),
                prompt: prompt
            });
            renderMusicHistory();
            
            if(data.message) {
                alert(data.message);
            }
        } catch (error) {
            console.error(error);
            alert("Generation Error: " + error.message);
        } finally {
            generateMusicBtn.disabled = false;
            musicBtnText.classList.remove('hidden');
            musicLoader.classList.add('hidden');
        }
    });

    function renderMusicHistory() {
        if(generatedMusicHistory.length === 0) {
            musicOutputsContainer.innerHTML = `
                <div class="empty-state">
                    <p>No tracks generated yet. Enter a prompt to start composing!</p>
                </div>
            `;
            return;
        }

        musicOutputsContainer.innerHTML = '';
        generatedMusicHistory.forEach((item, index) => {
            const el = document.createElement('div');
            el.className = 'output-item';
            el.innerHTML = `
                <div class="output-info">
                    <strong>AI Music Track</strong>
                    <span class="time">${item.timestamp}</span>
                </div>
                <div class="output-path" title="${item.prompt}">
                    Prompt: "${item.prompt}"
                </div>
                <div class="output-controls">
                    <audio controls src="${item.url}"></audio>
                </div>
            `;
            musicOutputsContainer.appendChild(el);
        });
    }

    // Init
    loadVoices();
});
