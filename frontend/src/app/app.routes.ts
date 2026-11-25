import { Component } from '@angular/core';
import { RouterLink, Routes } from '@angular/router';

@Component({
  selector: 'ot-login-placeholder',
  standalone: true,
  imports: [RouterLink],
  template: `
    <main class="ot-placeholder-page">
      <h1>OnTrack</h1>
      <p>Sign in shell placeholder.</p>
      <a routerLink="/privacy-and-terms-of-use">Privacy &amp; Terms of Use</a>
    </main>
  `,
})
export class LoginPlaceholderComponent {}

@Component({
  selector: 'ot-legal-placeholder',
  standalone: true,
  imports: [RouterLink],
  template: `
    <main class="ot-placeholder-page">
      <h1>Privacy &amp; Terms of Use</h1>
      <p>Legal terms placeholder.</p>
      <a routerLink="/login">Back to login page</a>
    </main>
  `,
})
export class LegalPlaceholderComponent {}

@Component({
  selector: 'ot-not-found-placeholder',
  standalone: true,
  imports: [RouterLink],
  template: `
    <main class="ot-placeholder-page">
      <h1>Page not found!</h1>
      <a routerLink="/">Return to home</a>
    </main>
  `,
})
export class NotFoundPlaceholderComponent {}

export const routes: Routes = [
  {
    path: '',
    pathMatch: 'full',
    redirectTo: 'login',
  },
  {
    path: 'login',
    component: LoginPlaceholderComponent,
    title: 'OnTrack — Login',
  },
  {
    path: 'privacy-and-terms-of-use',
    component: LegalPlaceholderComponent,
    title: 'OnTrack — Privacy & Terms of Use',
  },
  {
    path: '**',
    component: NotFoundPlaceholderComponent,
    title: 'OnTrack — Page Not Found',
  },
];
