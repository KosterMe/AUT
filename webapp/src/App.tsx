import { Routes, Route, Navigate } from "react-router-dom";
import Layout from "./components/Layout";
import AccountsPage from "./pages/Accounts";
import JobsPage from "./pages/Jobs";
import LibraryPage from "./pages/Library";
import JobDetailPage from "./pages/JobDetail";
import PublicationsPage from "./pages/Publications";
import QueuePage from "./pages/Queue";

export default function App() {
  return (
    <Layout>
      <Routes>
        <Route path="/" element={<Navigate to="/jobs" replace />} />
        <Route path="/jobs" element={<JobsPage />} />
        <Route path="/jobs/:jobId" element={<JobDetailPage />} />
        <Route path="/library" element={<LibraryPage />} />
        <Route path="/publications" element={<PublicationsPage />} />
        <Route path="/accounts" element={<AccountsPage />} />
        <Route path="/queue" element={<QueuePage />} />
      </Routes>
    </Layout>
  );
}
