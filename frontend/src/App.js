import React from 'react';
import { Navigate, Route, Routes, useLocation } from 'react-router-dom';
import { IntlProvider } from 'react-intl';

import './styles/ampay-main.css';
import 'remixicon/fonts/remixicon.css';

import Home from './routes/Home';
import Checkout from './routes/Checkout';
import Charging from './routes/Charging';
import Receipt from './routes/Receipt';
import Logo from './components/Logo';

export default function App() {
  const pathname = useLocation().pathname;

  const locale = navigator.language;

  // require the translation files
  const translations = {
    en: require('./lang/en.json'),
    de: require('./lang/de.json'),
  };

  // get the current locale and return the corresponding translation file
  const getCurrentTranslation = (locale) => {
    const language = locale.split(/[-_]/)[0];
    const messages = translations[language] || translations['en']; // fallback
    return messages;
  };

  return (
    <IntlProvider
      messages={getCurrentTranslation(locale)}
      locale={locale}
      defaultLocale="en"
    >
      <div
        className="ampay-container"
        style={pathname.startsWith('/receipt') ? { alignItems: 'unset', padding: 0, maxWidth: 'unset' } : {}}
      >
        {!pathname.startsWith('/receipt') && (
          <header className="ivora-header">
            <Logo size={34} />
          </header>
        )}
        <Routes>
          <Route path="/" exact={true} element={<Home />} />
          <Route path="/checkout/:evseId" exact={true} element={<Checkout />} />
          <Route
            path="/charging/:evseId/:sessionId"
            exact={true}
            element={<Charging />}
          />
          <Route
            path="/receipt/:evseId/:sessionId"
            exact={true}
            element={<Receipt />}
          />
          <Route path="*" element={<Navigate to="/" />} />
        </Routes>
        {!pathname.startsWith('/receipt') && (
          <footer className="ivora-footer">ivoracharge.com</footer>
        )}
      </div>
    </IntlProvider>
  );
}
