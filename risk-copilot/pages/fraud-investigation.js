import Head from 'next/head';
import { useState } from 'react';
import { api } from '../lib/api';

export default function FraudInvestigation() {
  const [customerId, setCustomerId] = useState('');
  const [result, setResult] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  async function investigate() {
    if (!customerId.trim()) return;
    setLoading(true);
    setError('');
    try {
      const res = await api.post('/api/agent_query', { customer_id: customerId.trim() });
      const data = await res.json();
      setResult(data);
    } catch (e) {
      setError(e.message || 'Investigation failed');
    } finally {
      setLoading(false);
    }
  }

  return (
    <>
      <Head><title>Fraud Investigation - Risk Copilot</title></Head>
      <main className="min-h-screen bg-slate-900 text-slate-100 p-6">
        <div className="max-w-5xl mx-auto">
          <h1 className="text-2xl font-bold mb-6">Fraud Investigation</h1>
          <div className="bg-slate-800 rounded-lg p-6 border border-slate-700 mb-6">
            <label className="block text-sm text-slate-400 mb-2">Customer ID</label>
            <div className="flex gap-3">
              <input
                value={customerId}
                onChange={(e) => setCustomerId(e.target.value)}
                placeholder="e.g. CUST_000001"
                className="flex-1 bg-slate-900 border border-slate-700 rounded px-4 py-2 focus:outline-none focus:border-emerald-500"
              />
              <button
                onClick={investigate}
                disabled={loading}
                className="bg-emerald-600 hover:bg-emerald-500 disabled:opacity-50 px-6 py-2 rounded font-medium"
              >
                {loading ? 'Analyzing...' : 'Investigate'}
              </button>
            </div>
            {error && <p className="text-red-400 text-sm mt-2">{error}</p>}
          </div>

          {result && (
            <div className="space-y-6">
              <div className="bg-slate-800 rounded-lg p-6 border border-slate-700">
                <h2 className="text-lg font-semibold mb-4">Risk Summary</h2>
                <div className="grid grid-cols-3 gap-4">
                  <div>
                    <div className="text-slate-400 text-sm">Risk Level</div>
                    <div className={`text-xl font-bold mt-1 ${
                      result.risk_level === 'high' ? 'text-red-400' :
                      result.risk_level === 'medium' ? 'text-amber-400' : 'text-emerald-400'
                    }`}>{result.risk_level?.toUpperCase()}</div>
                  </div>
                  <div>
                    <div className="text-slate-400 text-sm">Composite Score</div>
                    <div className="text-xl font-bold mt-1">{result.composite_fraud_score}</div>
                  </div>
                  <div>
                    <div className="text-slate-400 text-sm">Policies</div>
                    <div className="text-xl font-bold mt-1">{result.policy_count}</div>
                  </div>
                </div>
                <div className="mt-4">
                  <div className="text-slate-400 text-sm mb-1">Factors</div>
                  <div className="flex flex-wrap gap-2">
                    {(result.factors || []).map((f, i) => (
                      <span key={i} className="bg-slate-700 text-xs px-2 py-1 rounded">{f}</span>
                    ))}
                  </div>
                </div>
              </div>

              {result.signals && (
                <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
                  {Object.entries(result.signals).map(([key, signal]) => (
                    <div key={key} className="bg-slate-800 rounded-lg p-4 border border-slate-700">
                      <div className="text-sm text-slate-400 capitalize">{key.replace('_', ' ')}</div>
                      <div className="text-2xl font-bold mt-2">{signal.fraud_score}</div>
                      <div className="text-xs text-slate-500 mt-1">{signal.reason}</div>
                    </div>
                  ))}
                </div>
              )}

              {result.reports && (result.reports.sar || result.reports.ctr) && (
                <div className="bg-slate-800 rounded-lg p-6 border border-slate-700">
                  <h2 className="text-lg font-semibold mb-4">Generated Reports</h2>
                  {result.reports.sar && (
                    <div className="mb-3 p-3 bg-amber-900/30 border border-amber-700 rounded">
                      <div className="font-medium">SAR: {result.reports.sar.report_id}</div>
                      <div className="text-sm text-slate-300">{result.reports.sar.suspicious_activity.summary}</div>
                    </div>
                  )}
                  {result.reports.ctr && (
                    <div className="p-3 bg-blue-900/30 border border-blue-700 rounded">
                      <div className="font-medium">CTR: {result.reports.ctr.report_id}</div>
                      <div className="text-sm text-slate-300">Cash transactions reported</div>
                    </div>
                  )}
                </div>
              )}
            </div>
          )}
        </div>
      </main>
    </>
  );
}