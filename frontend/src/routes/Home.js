import React from 'react';
import { Button } from 'antd-mobile';
import { Input, Form } from 'antd';
import { useIntl } from 'react-intl';
import { useNavigate, useLocation } from 'react-router-dom';

import axios from '../util/Api.js';
import PaymentOptions from '../components/PaymentOptions.js';

export default function Home() {
  const [form] = Form.useForm();
  const intl = useIntl();
  const navigate = useNavigate();
  const location = useLocation();

  React.useEffect(() => {
    /* Setting evseId and errorMsg if we got forwarded from Checkout */
    const { evseId, errMsg } = location.state ? location.state : {};
    if (evseId) {
      form.setFields([
        {
          name: 'evseId',
          errors: [intl.formatMessage({ id: errMsg })],
          value: evseId,
        },
      ]);
    }
  }, [form, intl, location]);

  const onFinish = (values) => {
    axios
      .get(`evses/${values.evseId}`)
      .then(({ data }) => {
        // Check if location given, else show message
        if (data.id) {
          navigate(
            '/checkout/' + values.evseId,
            // { state: { locationData: {...data.data} } }
          );
        } else {
          form.setFields([
            {
              name: 'evseId',
              errors: [
                intl.formatMessage({
                  id: data.message ? data.message : 'global.error.generic',
                }),
              ],
            },
          ]);
        }
      })
      .catch((error) => {
        form.setFields([
          {
            name: 'evseId',
            errors: [
              intl.formatMessage({
                id: error.response?.data?.detail
                  ? error.response.data.detail
                  : 'global.error.generic',
              }),
            ],
          },
        ]);
      });
  };

  return (
    <div className="page-container page-container-home">
      <h1>{intl.formatMessage({ id: 'home.headline' })}</h1>
      <div className="home-hero-sub">
        {intl.formatMessage({ id: 'home.subheading' })}
      </div>

      <div className="home-card">
        <Form
          name="openinghours"
          onFinish={onFinish}
          form={form}
          className="width-100"
          labelCol={{ span: 0 }}
          wrapperCol={{ span: 24 }}
        >
          <Form.Item name="evseId" label="EVSE ID" rules={[{ required: true }]}>
            <Input
              style={{ width: '100%' }}
              size="large"
              placeholder="Charger ID, e.g. cp001-1"
            />
          </Form.Item>
          <Button type="submit" block>
            {intl.formatMessage({ id: 'global.continue' })}
          </Button>
        </Form>

        <div className="pay-options on-dark" style={{ marginBottom: 0 }}>
          <PaymentOptions />
        </div>
      </div>
    </div>
  );
}
