/**
 * Translation system for Entity Manager UI
 */
/**
 * Translation files live under a per-page-load version segment. Proxies in
 * front of Home Assistant may cache by path and ignore query strings, so the
 * path itself has to change.
 */
function translationUrl(lang) {
    const version = window.assetVersion || Date.now();
    return `static/translations/${version}/${lang}.json?v=${Date.now()}`;
}

class TranslationManager {
    constructor() {
        this.translations = {};
        this.currentLang = 'en';
        this.fallbackLang = 'en';
    }

    /**
     * Initialize the translation system
     * Detects user language and loads translations
     */
    async init() {
        // Try to get language from Home Assistant
        const haLang = await this.getHALanguage();

        // Fallback to browser language
        const browserLang = navigator.language || navigator.userLanguage || 'en';

        // Use HA language if available, otherwise browser language
        const lang = haLang || browserLang.split('-')[0];

        // Load translations
        await this.loadLanguage(lang);

        return this;
    }

    /**
     * Try to get Home Assistant's configured language
     */
    async getHALanguage() {
        // Home Assistant is where a user sets their language; this app follows it.
        // A lang parameter still wins, for trying a language out.
        try {
            const urlLang = new URLSearchParams(window.location.search).get('lang');
            if (urlLang) return urlLang;
        } catch (e) { /* no search params */ }
        try {
            const response = await fetch('api/ha/language');
            if (response.ok) {
                const data = await response.json();
                if (data.language) return data.language;
            }
        } catch (e) {
            console.warn('Could not read the Home Assistant language:', e);
        }
        return null;
    }

    /**
     * Load translations for a specific language
     */
    async loadLanguage(lang) {
        try {
            const response = await fetch(translationUrl(lang));
            if (response.ok) {
                this.translations = await response.json();
                this.currentLang = lang;
            } else {
                await this.loadFallback();
            }
        } catch (error) {
            console.error(`Error loading language ${lang}:`, error);
            await this.loadFallback();
        }
    }

    /**
     * Load fallback language (English)
     */
    async loadFallback() {
        try {
            const response = await fetch(translationUrl(this.fallbackLang));
            if (response.ok) {
                this.translations = await response.json();
                this.currentLang = this.fallbackLang;
            } else {
                console.error('Failed to load fallback language');
                this.translations = {};
            }
        } catch (error) {
            console.error('Error loading fallback language:', error);
            this.translations = {};
        }
    }

    /**
     * Get a translation by key path (e.g., "header.title")
     */
    t(key, params = {}) {
        const keys = key.split('.');
        let value = this.translations;

        for (const k of keys) {
            if (value && typeof value === 'object' && k in value) {
                value = value[k];
            } else {
                return key; // Return the key if translation not found
            }
        }

        // Replace parameters like {count}
        if (typeof value === 'string') {
            return value.replace(/{(\w+)}/g, (match, param) => {
                return params[param] !== undefined ? params[param] : match;
            });
        }

        return value;
    }

    /**
     * Get current language
     */
    getCurrentLanguage() {
        return this.currentLang;
    }

    /**
     * Current language getter for easy access
     */
    get currentLanguage() {
        return this.currentLang;
    }

    /**
     * Get available languages from backend
     */
    async getAvailableLanguages() {
        try {
            const response = await fetch('api/languages');
            if (response.ok) {
                const data = await response.json();
                return data.languages || [];
            }
        } catch (error) {
            console.error('Error fetching available languages:', error);
        }
        // Fallback
        return [{ code: 'en', name: 'English' }];
    }

    /**
     * Switch to a different language
     */
    async switchLanguage(lang) {
        await this.loadLanguage(lang);
        // Trigger UI update
        window.dispatchEvent(new CustomEvent('languageChanged', { detail: { language: lang } }));
    }
}

// Create global instance
window.translations = new TranslationManager();
