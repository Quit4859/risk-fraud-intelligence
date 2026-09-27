import Head from 'next/head';
import Link from 'next/link';
import { useEffect, useState } from 'react';
import { api } from '../lib/api';

export default function Home() {
  const [stats, setStats] = useState({
    totalCustomers: 0,
    highRisk: 0,
    mediumRisk: 0,
    lowRisk: 0,
    sarPending: 0,
    ctrPending: 0,
    loading: true
  });

  useEffect(() => {
    loadStats();
  }, []);

  async function loadStats() {
    try {
      const res = await api.get('/api/policy_update');
      const data = await res.json();
      setStats(prev => ({ ...prev, policies: data.policies, loading: false }));
    } catch (e) {
      setStats(prev => ({ ...prev, loading: false }));
    }
  }

  return (
    <>
      <Head>
        <title>Risk, Fraud & Regulatory Intelligence Copilot</title>
        <meta name="description" content="Banking compliance and fraud detection" />
      </Head>
      <main className="min-h-screen bg-slate-900 text-slate-100">
        <nav className="bg-slate-800 border-b border-slate-700 px-6 py-4">
          <div className="max-w-7xl mx-auto flex items-center justify-between">
            <Link href="/" className="text-xl font-bold text-emerald-400">
              ⚖️ Risk Copilot
            </Link>
            <div className="flex gap-4 text-sm">
              <Link href="/fraud-investigation" className="hover:text-emerald-400">Investigations</Link>
              <Link href="/compliance-review" className="hover:text-emerald-400">Compliance</Link>
              <Link href="/agent-chat" className="hover:text-emerald-400">Agent Chat</Link>
              <Link href="/audit-trail" className="hover:text-emerald-400">Audit Trail</Link>
            </div>
          </div>
        </nav>

        <div className="max-w-7xl mx-auto px-6 py-8">
          <h1 className="text-3xl font-bold mb-2">Risk Intelligence Dashboard</h1>
          <p className="text-slate-400 mb-8">Real-time fraud detection & regulatory compliance monitoring</p>

          <div className="grid grid-cols-1 md:grid-cols-4 gap-4 mb-8">
            <div className="bg-slate-800 rounded-lg p-6 border border-slate-700">
              <div className="text-slate-400 text-sm">Total Customers</div>
              <div className="text-3xl font-bold mt-2">50K+</div>
              <div className="text-xs text-emerald-400 mt-1">Nightly batch</div>
            </div>
            <div className="bg-slate-800 rounded-lg p-6 border border-slate-700">
              <div className="text-slate-400 text-sm">High Risk</div>
              <div className="text-3xl font-bold mt-2 text-red-400">{stats.highRisk}</div>
              <div className="text-xs text-slate-500 mt-1">Requires EDD</div>
            </div>
            <div className="bg-slate-800 rounded-lg p-6 border border-slate-700">
              <div className="text-slate-400 text-sm">SAR Pending</div>
              <div className="text-3xl font-bold mt-2 text-amber-400">{stats.sarPending}</div>
              <div className="text-xs text-slate-500 mt-1">Suspicious Activity Reports</div>
            </div>
            <div className="bg-slate-800 rounded-lg p-6 border border-slate-700">
              <div className="text-slate-400 text-sm">CTR Pending</div>
              <div className="text-3xl font-bold mt-2 text-blue-400">{stats.ctrPending}</div>
              <div className="text-xs text-slate-500 mt-1">Cash Transaction Reports</div>
            </div>
          </div>

          <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
            <div className="bg-slate-800 rounded-lg p-6 border border-slate-700">
              <h2 className="text-lg font-semibold mb-4">Quick Actions</h2>
              <div className="grid grid-cols-2 gap-3">
                <Link href="/fraud-investigation" className="bg-emerald-600 hover:bg-emerald-500 rounded px-4 py-3 text-center text-sm font-medium">
                  Start Investigation
                </Link>
                <Link href="/agent-chat" className="bg-blue-600 hover:bg-blue-500 rounded px-4 py-3 text-center text-sm font-medium">
                  Ask Copilot
                </Link>
                <Link href="/compliance-review" className="bg-amber-600 hover:bg-amber-500 rounded px-4 py-3 text-center text-sm font-medium">
                  Compliance Review
                </Link>
                <Link href="/audit-trail" className="bg-purple-600 hover:bg-purple-500 rounded px-4 py-3 text-center text-sm font-medium">
                  Audit Trail
                </Link>
              </div>
            </div>
            <div className="bg-slate-800 rounded-lg p-6 border border-slate-700">
              <h2 className="text-lg font-semibold mb-4">System Status</h2>
              <div className="space-y-3">
                <div className="flex justify-between"><span>Fraud Detection</span><span className="text-emerald-400">Operational</span></div>
                <div className="flex justify-between"><span>Policy Matcher</span><span className="text-emerald-400">Operational</span></div>
                <div className="flex justify-between"><span>Evidence Gatherer</span><span className="text-emerald-400">Operational</span></div>
                <div className="flex justify-between"><span>Report Formatter</span><span className="text-emerald-400">Operational</span></div>
                <div className="flex justify-between"><span>Escalation (Jira/Slack)</span><span className="text-amber-400">Degraded</span></div>
              </div>
            </div>
          </div>
        </div>
      </main>
    </>
  );
}