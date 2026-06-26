import * as React from 'react';
import IvoraIcon from '../assets/images/ivora-icon.png';

/**
 * Ivora Charge lockup: elephant icon mark (light/ivory variant) + wordmark.
 * `size` controls the icon badge in px. Set `wordmark={false}` for icon-only.
 */
export default function Logo({ size = 36, wordmark = true }) {
  return (
    <div className="ivora-logo">
      <img
        className="ivora-logo__badge"
        src={IvoraIcon}
        alt="Ivora Charge"
        style={{ width: size, height: size }}
      />
      {wordmark && (
        <div className="ivora-logo__wm">
          Ivora<span>Charge</span>
        </div>
      )}
    </div>
  );
}
